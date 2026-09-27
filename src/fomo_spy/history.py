"""Resumable chain evidence. Provider discovery is never coverage proof."""

from __future__ import annotations

import hashlib
from datetime import datetime
from decimal import InvalidOperation

import httpx
from sqlalchemy import select

from .db import CoverageGap, EvidenceFill, HistoricalValuation, ProviderSwap, ScanCheckpoint
from .domain import D, Fill, now
from .normalization import NATIVE_PRICE, TRANSFER, evm_movements, solana_movements, topic
from .providers import Unavailable
from .ranking import rank
from .rpc import RPC


def read(store, model, key, default=None):
    with store.session() as session:
        row = session.get(model, key)
        return row.data if row else default


def write(store, model, key, data):
    with store.session() as session:
        session.merge(model(key=key, data=data))


def failure(exc):
    # Endpoint URLs may contain credentials. Never persist arbitrary exception text.
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, Unavailable):
        return str(exc)
    return type(exc).__name__


def provider_rows(pages):
    """Deduplicate the authenticated swap schema, preserving raw legs/provenance."""
    rows = {}
    for page in pages:
        for row in page.get("swaps", []):
            if row.get("swapId"):
                rows.setdefault(str(row["swapId"]), row)
    return list(rows.values())


def ingest_provider(store, trader, history):
    rows = provider_rows(history.get("pages", []))
    with store.session() as session:
        for row in rows:
            data = dict(row, provenance="authenticated FOMO swap; discovery only")
            try:
                data["timestamp"] = datetime.fromisoformat(
                    row["at"].replace("Z", "+00:00")
                ).timestamp()
                data["legs"] = [
                    {
                        "side": side,
                        "token": row[field]["address"],
                        "quantity": str(D(str(row[field]["amount"]))),
                        "reported_usd": row[field].get("usd"),
                        "fee_usd": None,
                    }
                    for field, side in (("tokenIn", "sell"), ("tokenOut", "buy"))
                ]
            except (ValueError, KeyError, TypeError, InvalidOperation):
                data["parse_gap"] = "invalid swap legs/timestamp"
            session.merge(
                ProviderSwap(key=trader + ":" + str(row["swapId"]), trader=trader, data=data)
            )
    return len(rows)


class Prices:
    def __init__(self, store, http):
        self.store, self.http = store, http

    async def get(self, asset, timestamp):
        if not asset:
            return None
        key = f"{asset}:{int(timestamp)}"
        cached = read(self.store, HistoricalValuation, key)
        if cached and (cached.get("price") is not None or now() - cached["received"] < 3600):
            return D(cached["price"]) if cached.get("price") is not None else None
        data = {
            "asset": asset,
            "requested_timestamp": int(timestamp),
            "received": now(),
            "provenance": "https://coins.llama.fi/prices/historical",
            "price": None,
        }
        try:
            response = await self.http.get(
                f"https://coins.llama.fi/prices/historical/{int(timestamp)}/{asset}",
                params={"searchWidth": "4h"},
            )
            response.raise_for_status()
            coin = response.json().get("coins", {}).get(asset, {})
            data["response"] = coin
            price = D(str(coin.get("price", 0)))
            if (
                price.is_finite()
                and price > 0
                and coin.get("confidence", 0) >= 0.9
                and abs(coin.get("timestamp", 0) - timestamp) <= 4 * 3600
            ):
                data.update(price=str(price), timestamp=coin["timestamp"])
            else:
                data["gap"] = "historical price absent, stale, or low confidence"
        except (httpx.HTTPError, ValueError, TypeError, InvalidOperation) as exc:
            data["gap"] = failure(exc)
        write(self.store, HistoricalValuation, key, data)
        return D(data["price"]) if data["price"] is not None else None


async def block_at(rpc, timestamp, head):
    lo, hi = 0, head
    while lo < hi:
        mid = (lo + hi + 1) // 2
        block = await rpc.call("eth_getBlockByNumber", [hex(mid), False])
        if not block:
            raise Unavailable("historical block unavailable")
        if int(block["timestamp"], 16) <= timestamp:
            lo = mid
        else:
            hi = mid - 1
    return lo


async def probe(rpc, wallet, at):
    """Method-level admission check, not a claim of whole-wallet coverage."""
    c = rpc.chain
    report = {"chain": c.name, "at": at, "checks": {}, "ready": False}

    async def check(name, method, params=None):
        try:
            result = await rpc.call(method, params)
            if result is None:
                raise Unavailable(f"RPC {method} returned null")
            report["checks"][name] = {"ok": True}
            return result
        except Exception as exc:
            report["checks"][name] = {"ok": False, "reason": failure(exc)}
            return None

    try:
        await rpc.check_network()
        report["checks"]["identity"] = {"ok": True}
    except Exception as exc:
        report["checks"]["identity"] = {"ok": False, "reason": failure(exc)}
        return report
    if c.kind == "evm":
        head = await check("head", "eth_blockNumber")
        if head is None:
            return report
        try:
            height = await block_at(rpc, at - 30 * 86400, int(head, 16))
        except Exception as exc:
            report["checks"]["window_block"] = {"ok": False, "reason": failure(exc)}
            return report
        report["window_block"] = height
        tag = hex(height)
        await check("historical_native_balance", "eth_getBalance", [wallet, tag])
        await check(
            "historical_token_balance",
            "eth_call",
            [{"to": c.settlement, "data": "0x70a08231" + wallet[2:].zfill(64)}, tag],
        )
        await check(
            "incoming_logs",
            "eth_getLogs",
            [{"fromBlock": tag, "toBlock": tag, "topics": [TRANSFER, None, topic(wallet)]}],
        )
        await check(
            "outgoing_logs",
            "eth_getLogs",
            [{"fromBlock": tag, "toBlock": tag, "topics": [TRANSFER, topic(wallet)]}],
        )
        for direction in ("fromAddress", "toAddress"):
            await check(
                "native_discovery_" + direction,
                "trace_filter",
                [{"fromBlock": tag, "toBlock": tag, direction: [wallet]}],
            )
        block = await check("historical_block", "eth_getBlockByNumber", [tag, False])
        if block and block.get("transactions"):
            tx = block["transactions"][0]
            await check("receipt", "eth_getTransactionReceipt", [tx])
            await check("native_trace", "debug_traceTransaction", [tx, {"tracer": "callTracer"}])
        else:
            report["checks"]["native_trace"] = {
                "ok": False,
                "reason": "no historical sample transaction",
            }
    else:
        await check("first_available", "getFirstAvailableBlock")
        rows = await check(
            "signatures",
            "getSignaturesForAddress",
            [wallet, {"limit": 2, "commitment": "finalized"}],
        )
        if rows:
            await check(
                "transaction",
                "getTransaction",
                [
                    rows[-1]["signature"],
                    {
                        "encoding": "jsonParsed",
                        "commitment": "finalized",
                        "maxSupportedTransactionVersion": 0,
                    },
                ],
            )
        else:
            report["checks"]["transaction"] = {
                "ok": False,
                "reason": "no finalized sample transaction",
            }
        await check(
            "token_accounts",
            "getTokenAccountsByOwner",
            [
                wallet,
                {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
                {"encoding": "jsonParsed", "commitment": "finalized"},
            ],
        )
    report["ready"] = all(x["ok"] for x in report["checks"].values())
    return report


class History:
    def __init__(self, cfg, store, http, health):
        self.cfg, self.store, self.http, self.health = cfg, store, http, health
        self.prices = Prices(store, http)

    async def persist(self, movement):
        sig = movement.signal
        price = await self.prices.get(movement.price_asset, sig.timestamp)
        gas_price = (
            await self.prices.get(NATIVE_PRICE.get(sig.chain), sig.timestamp)
            if movement.gas_native
            else None
        )
        fee = (
            D(0)
            if movement.gas_native == 0
            else movement.gas_native * gas_price
            if movement.gas_native is not None and gas_price is not None
            else None
        )
        fill = Fill(
            id=sig.key,
            trader=sig.trader,
            chain=sig.chain,
            token=sig.token,
            side=sig.side,
            quantity=sig.quantity,
            usd=movement.consideration * price
            if price is not None and movement.consideration is not None
            else None,
            fee_usd=fee,
            timestamp=sig.timestamp,
            tx=sig.tx,
            provenance="canonical RPC wallet deltas; historical DefiLlama consideration; gas separate",
        )
        data = {
            "fill": fill.model_dump(mode="json"),
            "signal": sig.model_dump(mode="json"),
            "consideration": str(movement.consideration)
            if movement.consideration is not None
            else None,
            "price_asset": movement.price_asset,
            "gas_native": str(movement.gas_native) if movement.gas_native is not None else None,
        }
        with self.store.session() as session:
            previous = session.get(EvidenceFill, sig.key)
            if previous and previous.data["signal"]["block_hash"] != sig.block_hash:
                raise Unavailable("historical transaction changed block identity; audit required")
            session.merge(
                EvidenceFill(
                    key=sig.key,
                    trader=sig.trader,
                    chain=sig.chain,
                    timestamp=sig.timestamp,
                    data=data,
                )
            )
        self.store.put("last_history_data", {"at": now(), "chain": sig.chain, "tx": sig.tx})

    def evaluate(self, trader, at=None):
        at = now() if at is None else at
        uid = trader["userId"]
        coverage, gaps = {}, []
        for chain in self.cfg.chains:
            if not chain.enabled:
                continue
            wallet = trader.get("wallets", {}).get("solana" if chain.kind == "solana" else "evm")
            key = f"{uid}:{chain.name}:{wallet}"
            state = read(self.store, ScanCheckpoint, key, {})
            gap = read(self.store, CoverageGap, key, {})
            complete = (
                state.get("complete", False)
                and state.get("through", 0) >= at - 300
                and state.get("start", at) <= at - 30 * 86400
                and not gap.get("reason")
            )
            coverage[chain.name] = dict(state, complete=complete, blocker=gap.get("reason"))
            if not complete:
                gaps.append(
                    f"{chain.name}: " + (gap.get("reason") or "history reconstruction pending")
                )
        with self.store.session() as session:
            fills = [
                Fill.model_validate(r.data["fill"])
                for r in session.scalars(select(EvidenceFill).where(EvidenceFill.trader == uid))
            ]
        evidence = rank(fills, complete_30d=not gaps, at=at)
        evidence.update(
            coverage=coverage,
            missing=gaps,
            state="data unavailable"
            if any(v.get("blocker") for v in coverage.values())
            else "history reconstruction in progress"
            if gaps
            else "evaluation complete",
        )
        if gaps:
            # These are evidence blockers, not an evaluated unprofitable trader.
            evidence["provisional_reasons"] = evidence["reasons"]
            evidence["reasons"] = ["30-day chain evidence incomplete"]
        self.store.put("ranking:" + uid, evidence)
        return evidence

    async def step(self, trader):
        uid = trader["userId"]
        ingest_provider(self.store, uid, self.store.get("history_raw:" + uid, {}))
        for c in self.cfg.chains:
            if not c.enabled:
                continue
            wallet = trader.get("wallets", {}).get("solana" if c.kind == "solana" else "evm")
            key = f"{uid}:{c.name}:{wallet}"
            if not wallet:
                write(self.store, CoverageGap, key, {"reason": "wallet unavailable", "at": now()})
                continue
            rpc = RPC(c, self.http, historical=True)
            endpoint = hashlib.sha256(rpc.endpoint.encode()).hexdigest()
            cap_key = "history_capability:" + c.name
            cap = self.store.get(cap_key)
            if not cap or now() - cap["at"] > 3600 or cap.get("endpoint") != endpoint:
                cap = await probe(rpc, wallet, now())
                cap["endpoint"] = endpoint
                self.store.put(cap_key, cap)
            self.health("history:" + c.name, cap)
            if not cap["ready"]:
                reason = "; ".join(
                    k + ": " + v.get("reason", "unavailable")
                    for k, v in cap["checks"].items()
                    if not v["ok"]
                )
                write(self.store, CoverageGap, key, {"reason": reason, "at": now()})
                continue
            try:
                if c.kind == "evm":
                    await self.evm_step(rpc, trader, wallet, key, cap)
                else:
                    await self.solana_step(rpc, trader, wallet, key)
            except Exception as exc:
                write(self.store, CoverageGap, key, {"reason": failure(exc), "at": now()})
        return self.evaluate(trader)

    async def evm_step(self, rpc, trader, wallet, key, cap):
        state = read(self.store, ScanCheckpoint, key)
        head = int(await rpc.call("eth_blockNumber"), 16) - rpc.chain.confirmations
        if state is None:
            start = cap["window_block"]
            state = {
                "lower": start,
                "next": start,
                "target": head,
                "complete": False,
                "start": now() - 30 * 86400,
                "transactions": 0,
                "tokens": [],
            }
        if state.get("hash"):
            old = await rpc.call("eth_getBlockByNumber", [hex(state["next"] - 1), False])
            if not old or old["hash"] != state["hash"]:
                state["complete"] = False
                write(self.store, ScanCheckpoint, key, state)
                raise Unavailable("historical checkpoint reorganized; audit/rebuild required")
        lo = state["next"]
        target = state.get("backfill_end", head)
        end = min(target, lo + self.cfg.history_batch_blocks - 1)
        if end < lo:
            return
        query = {"fromBlock": hex(lo), "toBlock": hex(end)}
        logs = {}
        for topics in ([TRANSFER, topic(wallet)], [TRANSFER, None, topic(wallet)]):
            page = await rpc.call("eth_getLogs", [{**query, "topics": topics}])
            if len(page) >= 10000:
                raise Unavailable("log range saturated; smaller history_batch_blocks required")
            for log in page:
                if log.get("removed"):
                    raise Unavailable("removed log during historical scan")
                logs[(log["transactionHash"], log["logIndex"])] = log
        txids = {log["transactionHash"] for log in logs.values()}
        # trace_filter enumerates internal native receipts too. A provider may cap
        # results: request a bounded count and refuse a saturated response.
        for direction in ("fromAddress", "toAddress"):
            traces = await rpc.call(
                "trace_filter", [{**query, direction: [wallet], "count": 10000}]
            )
            if len(traces) >= 10000:
                raise Unavailable("trace range saturated; smaller history_batch_blocks required")
            txids.update(t["transactionHash"] for t in traces if t.get("transactionHash"))
        receipts = []
        for txid in txids:
            receipt = await rpc.call("eth_getTransactionReceipt", [txid])
            if not receipt:
                raise Unavailable("historical receipt unavailable")
            receipts.append(receipt)
        receipts.sort(key=lambda r: (int(r["blockNumber"], 16), int(r["transactionIndex"], 16)))
        for receipt in receipts:
            block = await rpc.call("eth_getBlockByNumber", [receipt["blockNumber"], False])
            trace = await rpc.call(
                "debug_traceTransaction", [receipt["transactionHash"], {"tracer": "callTracer"}]
            )
            movements = await evm_movements(
                rpc,
                receipt,
                block,
                trader["userId"],
                wallet,
                list(logs.values()),
                float("inf"),
                trace,
            )
            for movement in movements:
                await self.persist(movement)
                if movement.signal.token not in state["tokens"]:
                    state["tokens"].append(movement.signal.token)
        last = await rpc.call("eth_getBlockByNumber", [hex(end), False])
        state.update(
            next=end + 1,
            hash=last["hash"],
            through=int(last["timestamp"], 16),
            target=head,
            transactions=state["transactions"] + len(receipts),
            complete=False,
        )
        write(self.store, ScanCheckpoint, key, state)
        write(self.store, CoverageGap, key, {})
        if end == target:
            # Every token must have a zero opening inventory; extend backwards
            # until its earlier acquisition is included. Never invent opening cost.
            opening = [
                await rpc.balance(t, wallet, hex(max(0, state["lower"] - 1)))
                for t in state["tokens"]
            ]
            if any(opening):
                if state["lower"] == 0:
                    raise Unavailable("nonzero genesis inventory has unknown cost")
                state.update(
                    next=max(0, state["lower"] - self.cfg.history_batch_blocks),
                    hash=None,
                    backfill_end=state["lower"] - 1,
                    resume_head=state.get("resume_head", head),
                )
                state["lower"] = state["next"]
                block = await rpc.call("eth_getBlockByNumber", [hex(state["lower"]), False])
                state["start"] = int(block["timestamp"], 16)
            else:
                reconcile_head = state.pop("resume_head", head)
                await self.reconcile_evm(rpc, trader["userId"], wallet, reconcile_head, state)
                last = await rpc.call("eth_getBlockByNumber", [hex(reconcile_head), False])
                state.pop("backfill_end", None)
                state.update(
                    next=reconcile_head + 1, hash=last["hash"], through=int(last["timestamp"], 16)
                )
                state["complete"] = True
            write(self.store, ScanCheckpoint, key, state)

    async def reconcile_evm(self, rpc, uid, wallet, head, state):
        with self.store.session() as session:
            rows = list(
                session.scalars(
                    select(EvidenceFill).where(
                        EvidenceFill.trader == uid, EvidenceFill.chain == rpc.chain.name
                    )
                )
            )
        totals = {}
        for row in rows:
            sig = row.data["signal"]
            if sig["source_before"] is None or sig["source_after"] is None:
                raise Unavailable("missing inventory delta")
            totals[sig["token"]] = (
                totals.get(sig["token"], D(0)) + D(sig["source_after"]) - D(sig["source_before"])
            )
        for token, quantity in totals.items():
            decimals = await rpc.decimals(token, hex(head))
            actual = D(await rpc.balance(token, wallet, hex(head))) / 10**decimals
            if actual != quantity:
                raise Unavailable("closing inventory mismatch: " + token)

    async def solana_step(self, rpc, trader, wallet, key):
        state = read(
            self.store,
            ScanCheckpoint,
            key,
            {
                "before": None,
                "transactions": 0,
                "complete": False,
                "start": now(),
                "accounts": [wallet],
            },
        )
        params = {"limit": 100, "commitment": "finalized"}
        if state["before"]:
            params["before"] = state["before"]
        page = await rpc.call("getSignaturesForAddress", [wallet, params])
        if page and page[-1]["signature"] == state["before"]:
            raise Unavailable("repeated Solana signature page")
        for row in page:
            if row.get("err"):
                continue
            tx = await rpc.call(
                "getTransaction",
                [
                    row["signature"],
                    {
                        "encoding": "jsonParsed",
                        "commitment": "finalized",
                        "maxSupportedTransactionVersion": 0,
                    },
                ],
            )
            if not tx or tx.get("blockTime") is None:
                raise Unavailable("finalized Solana transaction or timestamp unavailable")
            for movement in solana_movements(
                tx, trader["userId"], wallet, row["signature"], rpc.chain, float("inf")
            ):
                await self.persist(movement)
            state["start"] = min(state["start"], tx["blockTime"])
        if page:
            state["before"] = page[-1]["signature"]
            state["transactions"] += len(page)
        write(self.store, ScanCheckpoint, key, state)
        # Standard owner RPC cannot enumerate already-closed token accounts. An
        # empty wallet-signature page is not proof of their full transfer history.
        write(
            self.store,
            CoverageGap,
            key,
            {
                "at": now(),
                "reason": "Solana closed token-account enumeration and historical inventory reconciliation unavailable",
            },
        )
