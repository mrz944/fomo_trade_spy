"""Reconciled current Solana inventory; no claims about pre-activation history."""

from __future__ import annotations

import copy
from collections import defaultdict

from sqlalchemy import select

from .db import Event, MonitorState, Position
from .domain import D, Signal, now
from .normalization import WSOL, Movement, solana_movements
from .providers import Unavailable


class ScanYield(Exception):
    """A bounded turn completed; persisted work continues next turn."""


TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"


def keys(tx):
    return [
        row["pubkey"] if isinstance(row, dict) else row
        for row in tx["transaction"]["message"]["accountKeys"]
    ]


def accounts(tx, wallet, field):
    addresses = keys(tx)
    return {
        addresses[row["accountIndex"]]: {
            "mint": row["mint"],
            "amount": int(row["uiTokenAmount"]["amount"]),
            "decimals": row["uiTokenAmount"]["decimals"],
            "program": row.get("programId", TOKEN),
        }
        for row in tx["meta"].get(field, [])
        if row.get("owner") == wallet
    }


class SolanaCurrent:
    def __init__(self, monitor):
        self.monitor, self.store = monitor, monitor.store

    def save(self, key, data):
        with self.store.session() as session:
            session.merge(MonitorState(key=key, data=data))

    async def snapshot(self, rpc, wallet, program):
        result = await rpc.call(
            "getTokenAccountsByOwner",
            [wallet, {"programId": program}, {"encoding": "jsonParsed", "commitment": "finalized"}],
        )
        values = {}
        for row in result["value"]:
            info = row["account"]["data"]["parsed"]["info"]
            if info["owner"] != wallet or row["account"]["owner"] != program:
                raise Unavailable("token account snapshot owner/program mismatch")
            values[row["pubkey"]] = {
                "mint": info["mint"],
                "amount": int(info["tokenAmount"]["amount"]),
                "decimals": info["tokenAmount"]["decimals"],
                "program": program,
            }
        return int(result["context"]["slot"]), values

    async def fetch(self, rpc, method, params):
        if self.requests >= 40:
            raise ScanYield()
        self.requests += 1
        return await rpc.call(method, params)

    def progress(self):
        self.state["version"] = 2
        self.state["last_progress"] = now()
        self.state.pop("blocking_reason", None)
        self.state.pop("retry_at", None)
        self.save(self.key, self.state)

    async def signatures(self, rpc, address, lower, upper):
        scan = self.state["scan"]
        cursor = scan["pages"].setdefault(
            address, {"before": None, "seen": [], "rows": {}, "done": False}
        )
        while not cursor["done"]:
            if self.pages >= 10:
                raise ScanYield()
            options = {"limit": 100, "commitment": "finalized"}
            if cursor["before"]:
                options["before"] = cursor["before"]
            page = await self.fetch(rpc, "getSignaturesForAddress", [address, options])
            self.pages += 1
            seen = set(cursor["seen"])
            for row in page:
                if row["signature"] in seen:
                    raise Unavailable("repeated Solana monitoring signature page")
                seen.add(row["signature"])
                if row["slot"] <= lower:
                    cursor["done"] = True
                    break
                if row["slot"] <= upper and not row.get("err"):
                    cursor["rows"][row["signature"]] = row
            if not page:
                first = await self.fetch(rpc, "getFirstAvailableBlock", [])
                if first is None or first > lower:
                    raise Unavailable("Solana signature history ended before checkpoint")
                cursor["done"] = True
            cursor["before"] = page[-1]["signature"] if page else cursor["before"]
            cursor["seen"] = list(seen)
            self.progress()
        return cursor["rows"]

    def evidence_key(self, kind, identity):
        scan = self.state["scan"]
        return f"fetch:{self.key}:{scan['lower']}:{scan['slot']}:{kind}:{identity}"

    def evidence(self, kind, identity):
        with self.store.session() as session:
            row = session.get(MonitorState, self.evidence_key(kind, identity))
            return row.data["value"] if row else None

    def save_evidence(self, kind, identity, value):
        self.state["last_progress"] = now()
        self.save(self.evidence_key(kind, identity), {"version": 2, "at": now(), "value": value})

    async def transaction(self, rpc, signature):
        from .rpc import SOLANA_READ_VERSION

        cached = self.evidence("transaction", signature)
        if cached is not None:
            return cached
        tx = await self.fetch(
            rpc,
            "getTransaction",
            [
                signature,
                {
                    "encoding": "jsonParsed",
                    "commitment": "finalized",
                    "maxSupportedTransactionVersion": SOLANA_READ_VERSION,
                },
            ],
        )
        if not tx or tx.get("blockTime") is None or tx.get("meta") is None:
            raise Unavailable("finalized Solana transaction unavailable")
        expected = next(
            (
                c["rows"][signature]["slot"]
                for c in self.state["scan"]["pages"].values()
                if signature in c["rows"]
            ),
            None,
        )
        if tx["slot"] != expected or tx["meta"].get("err"):
            raise Unavailable("Solana transaction conflicts with signature evidence")
        self.save_evidence("transaction", signature, tx)
        return tx

    async def scan(self, rpc, trader, wallet):
        from .rpc import RPCBackoff

        self.monitor.new_activation(rpc.chain.name, trader)
        self.requests = self.pages = 0
        errors = []
        for program in (TOKEN, TOKEN_2022):
            try:
                await self.scan_program(rpc, trader, wallet, program)
            except ScanYield:
                self.save(self.key, self.state)
                break
            except Unavailable as exc:
                if self.state:
                    self.state["blocking_reason"] = str(exc)
                    self.state["retry_at"] = now() + getattr(exc, "seconds", 0)
                    self.save(self.key, self.state)
                errors.append(exc)
                if isinstance(exc, RPCBackoff):
                    break
        states = []
        with self.store.session() as session:
            for program in (TOKEN, TOKEN_2022):
                row = session.get(MonitorState, f"solana:{trader}:{wallet}:{program}")
                states.append(copy.deepcopy(row.data) if row else {})
        covered = min(
            (
                s.get("scan", {}).get("covered_at", 0)
                if s.get("scan", {}).get("stage") == "deliver"
                else s.get("covered_at", 0)
            )
            for s in states
        )
        reason = str(errors[0]) if errors else None
        if not reason and any(
            s.get("blocking_reason") or s.get("scan", {}).get("stage") not in (None, "deliver")
            for s in states
        ):
            reason = "scan in progress or reconciliation required"
        if not reason and covered < now() - 120:
            reason = "exit checkpoint stale"
        self.monitor.ready(
            rpc.chain.name,
            trader,
            min(s.get("anchor", 0) for s in states),
            reason,
            covered_at=covered,
        )
        # Both programs have been evaluated before an entry can be delivered.
        for program, state in zip((TOKEN, TOKEN_2022), states):
            scan = state.get("scan", {})
            if scan.get("stage") != "deliver":
                continue
            key = f"solana:{trader}:{wallet}:{program}"
            for delivery in scan["deliveries"]:
                if delivery.get("done"):
                    continue
                sig = Signal.model_validate(delivery["signal"])
                result = await self.monitor.emit_movement(
                    Movement(sig, None, None, None), recovery_key=key
                )
                if result == "recovery pending":
                    state["blocking_reason"] = (
                        "verified exit awaiting executable quote or reconciliation"
                    )
                    self.save(key, state)
                    self.monitor.ready(
                        rpc.chain.name, trader, reason=state["blocking_reason"], covered_at=covered
                    )
                    return
                delivery["done"] = True
                self.save(key, state)
            with self.store.session() as session:
                if scan["deliveries"]:
                    session.merge(
                        MonitorState(
                            key=f"evidence:{key}:{scan['lower']}:{scan['slot']}", data=state
                        )
                    )
                session.merge(
                    MonitorState(
                        key=key,
                        data={
                            "version": 2,
                            "slot": scan["slot"],
                            "anchor": state["anchor"],
                            "accounts": scan["accounts"],
                            "at": now(),
                            "covered_at": scan["covered_at"],
                            "activation": state.get("activation"),
                            "last_progress": now(),
                        },
                    )
                )
        if errors:
            raise errors[0]

    async def scan_program(self, rpc, trader, wallet, program):
        chain = rpc.chain.name
        key = f"solana:{trader}:{wallet}:{program}"
        with self.store.session() as session:
            row = session.get(MonitorState, key)
            state = copy.deepcopy(row.data) if row else None
            positions = list(
                session.scalars(
                    select(Position).where(
                        Position.mode == self.monitor.cfg.mode,
                        Position.chain == chain,
                        Position.trader == trader,
                    )
                )
            )
            open_mints = {p.token for p in positions if D(p.quantity) > 0}
        self.key, self.state = key, state
        if state and state.get("scan", {}).get("stage") == "deliver":
            return
        activation = self.store.get(f"monitor_epoch:{chain}:{trader}")
        if state and state.get("activation") != activation and not open_mints:
            state = None
        if state is None:
            if open_mints:
                raise Unavailable("open position has no inventory checkpoint")
            slot, snapshot = await self.snapshot(rpc, wallet, program)
            covered = await self.fetch(rpc, "getBlockTime", [slot])
            if covered is None:
                raise Unavailable("finalized snapshot time unavailable")
            self.save(
                key,
                {
                    "version": 2,
                    "covered_at": covered,
                    "slot": slot,
                    "anchor": slot,
                    "accounts": snapshot,
                    "at": now(),
                    "activation": activation,
                },
            )
            return
        self.state = state
        if "covered_at" not in state:
            state["covered_at"] = await self.fetch(rpc, "getBlockTime", [state["slot"]]) or 0
            self.progress()
        if not state.get("scan"):
            lower = state["slot"]
            with self.store.session() as session:
                for event in session.scalars(
                    select(Event).where(Event.status.in_(["rejected", "received"]))
                ):
                    data = event.data
                    if (
                        data.get("chain") == chain
                        and data.get("trader") == trader
                        and data.get("token") in open_mints
                        and data.get("side") == "sell"
                        and not data.get("recovery")
                        and event.reason
                        not in (
                            "no copied inventory for this source trader",
                            "sale precedes copied purchase",
                            "exit below token precision",
                        )
                    ):
                        lower = min(lower, data["block"] - 1)
            slot, snapshot = await self.snapshot(rpc, wallet, program)
            if slot < state["slot"]:
                raise Unavailable("finalized Solana snapshot moved backwards")
            state["scan"] = {
                "slot": slot,
                "lower": lower,
                "accounts": snapshot,
                "stage": "signatures",
                "pages": {},
                "blocks": {},
            }
            self.progress()
        scan = state["scan"]
        slot, snapshot = scan["slot"], scan["accounts"]
        if "covered_at" not in scan:
            covered = await self.fetch(rpc, "getBlockTime", [slot])
            if covered is None:
                raise Unavailable("finalized snapshot time unavailable")
            scan["covered_at"] = covered
            self.progress()
        signatures = await self.signatures(rpc, wallet, scan["lower"], slot)
        scan["stage"] = "transactions"
        transactions = {sig: await self.transaction(rpc, sig) for sig in signatures}
        relevant = set(open_mints)
        discovered = dict(state["accounts"])
        discovered.update(snapshot)
        for tx in transactions.values():
            for field in ("preTokenBalances", "postTokenBalances"):
                discovered.update(accounts(tx, wallet, field))
            relevant.update(
                m.signal.token
                for m in solana_movements(
                    tx, trader, wallet, "discovery", rpc.chain, self.monitor.started
                )
                if m.signal.side in ("buy", "sell")
            )
        relevant -= {rpc.chain.settlement, WSOL}
        relevant &= {a["mint"] for a in discovered.values() if a["program"] == program}
        # Token-account transfers need not mention the owner's wallet. Discover
        # every known account of a traded/copied mint, including closed accounts.
        scan["stage"] = "accounts"
        addresses = list(discovered)
        for address in addresses:
            data = discovered[address]
            if data["mint"] in relevant and data["program"] == program:
                rows = await self.signatures(rpc, address, scan["lower"], slot)
                for sig in rows.keys() - transactions.keys():
                    tx = transactions[sig] = await self.transaction(rpc, sig)
                    for field in ("preTokenBalances", "postTokenBalances"):
                        for found, info in accounts(tx, wallet, field).items():
                            if found not in discovered:
                                addresses.append(found)
                                discovered[found] = info
        by_slot = defaultdict(list)
        for sig, tx in transactions.items():
            by_slot[tx["slot"]].append((sig, tx))
        ordered = []
        scan["stage"] = "blocks"
        for height in sorted(by_slot):
            cached = self.evidence("block", height)
            block = cached or await self.fetch(
                rpc,
                "getBlock",
                [
                    height,
                    {
                        "commitment": "finalized",
                        "transactionDetails": "signatures",
                        "rewards": False,
                        "maxSupportedTransactionVersion": 1,
                    },
                ],
            )
            if not block or not block.get("blockhash") or not block.get("signatures"):
                raise Unavailable("canonical Solana block signatures unavailable")
            if cached is None:
                self.save_evidence("block", height, block)
            scan["blocks"][str(height)] = self.evidence_key("block", height)
            index = {sig: i for i, sig in enumerate(block["signatures"])}
            if any(sig not in index for sig, _ in by_slot[height]):
                raise Unavailable("Solana transaction absent from finalized block")
            ordered.extend(
                (sig, tx, block["blockhash"])
                for sig, tx in sorted(by_slot[height], key=lambda pair: index[pair[0]])
            )
        scan["stage"] = "reconcile"
        working = copy.deepcopy(state["accounts"])
        pending, gaps = [], set()
        # Revalidate previously rejected sales by reversing canonical evidence from
        # the committed inventory, checking every post-balance before undoing it.
        for _, tx, _ in reversed(ordered):
            if tx["slot"] > state["slot"]:
                continue
            pre = accounts(tx, wallet, "preTokenBalances")
            post = accounts(tx, wallet, "postTokenBalances")
            for address in pre.keys() | post.keys():
                info = pre.get(address, post.get(address))
                if info["mint"] not in relevant or info["program"] != program:
                    continue
                if working.get(address, {}).get("amount", 0) != post.get(address, {}).get(
                    "amount", 0
                ):
                    gaps.add(info["mint"])
                if address in pre:
                    working[address] = pre[address]
                else:
                    working.pop(address, None)
        for signature, tx, block_hash in ordered:
            pre = accounts(tx, wallet, "preTokenBalances")
            post = accounts(tx, wallet, "postTokenBalances")
            movements = solana_movements(
                tx, trader, wallet, signature, rpc.chain, self.monitor.started
            )
            totals = {
                mint: sum(a["amount"] for a in working.values() if a["mint"] == mint)
                for mint in relevant
            }
            for address in pre.keys() | post.keys():
                info = pre.get(address, post.get(address))
                mint = info["mint"]
                if mint not in relevant:
                    continue
                if info["program"] != program:
                    gaps.add(mint)
                    continue
                if working.get(address, {}).get("amount", 0) != pre.get(address, {}).get(
                    "amount", 0
                ):
                    gaps.add(mint)
                if address in post:
                    working[address] = post[address]
                else:
                    working.pop(address, None)
            for movement in movements:
                signal = movement.signal
                if signal.token not in relevant:
                    continue
                if signal.side == "transfer":
                    instructions = tx["transaction"]["message"].get("instructions", [])
                    allowed = {
                        TOKEN,
                        TOKEN_2022,
                        "11111111111111111111111111111111",
                        "ComputeBudget111111111111111111111111111111",
                        "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
                    }
                    explained = (
                        instructions
                        and all(i.get("programId") in allowed for i in instructions)
                        and any(
                            i.get("programId") == program
                            and i.get("parsed", {}).get("type") in ("transfer", "transferChecked")
                            and any(
                                i["parsed"].get("info", {}).get(k) in pre.keys() | post.keys()
                                for k in ("source", "destination")
                            )
                            for i in instructions
                        )
                    )
                    if not explained:
                        gaps.add(signal.token)
                before = D(totals.get(signal.token, 0)) / 10**signal.token_decimals
                after = (
                    D(sum(a["amount"] for a in working.values() if a["mint"] == signal.token))
                    / 10**signal.token_decimals
                )
                signal.source_before, signal.source_after, signal.block_hash = (
                    before,
                    after,
                    block_hash,
                )
                if abs(after - before) != signal.quantity:
                    gaps.add(signal.token)
                pending.append(movement)
        for mint in relevant:
            expected = {
                k: v["amount"] for k, v in snapshot.items() if v["mint"] == mint and v["amount"]
            }
            actual = {
                k: v["amount"] for k, v in working.items() if v["mint"] == mint and v["amount"]
            }
            if expected != actual:
                gaps.add(mint)
        if gaps:
            with self.store.session() as session:
                for position in session.scalars(
                    select(Position).where(
                        Position.mode == self.monitor.cfg.mode,
                        Position.chain == chain,
                        Position.trader == trader,
                    )
                ):
                    if position.token in gaps and D(position.quantity) > 0:
                        position.needs_reconcile = True
            self.monitor.health(
                f"inventory:{chain}:{trader}",
                {
                    "state": "reconciliation required",
                    "tokens": sorted(gaps),
                    "reason": "current token accounts do not reconcile or token program is unsupported",
                    "at": now(),
                },
            )
        if gaps:
            raise Unavailable("current token accounts do not reconcile; history gap retained")
        scan["stage"] = "deliver"
        scan["deliveries"] = [
            {"signal": m.signal.model_dump(mode="json"), "done": False} for m in pending
        ]
        self.progress()
