from __future__ import annotations

import asyncio
import json
import time
import weakref

import httpx
import websockets
from sqlalchemy import select

from .config import Chain, Settings
from .db import Position, ResearchSelection, Store
from .domain import now
from .normalization import TRANSFER, evm_movements, solana_movements, topic
from .normalization import log_delta as log_delta
from .providers import Unavailable
from .scheduler import EndpointBudget

# Solana v1 keeps canonical pre/post balances and meta.fee in jsonParsed.
# This is read support only; signing remains in the restricted live executor.
SOLANA_READ_VERSION = 1
RPC_LIMITS = weakref.WeakKeyDictionary()


class RPCBackoff(Unavailable):
    def __init__(self, seconds):
        self.seconds = seconds
        super().__init__(f"RPC rate limited; retry in {int(seconds)} seconds")


class RPC:
    def __init__(self, chain: Chain, http: httpx.AsyncClient, *, historical=False):
        self.chain, self.http = chain, http
        self.endpoint = (chain.historical_rpc or chain.rpc) if historical else chain.rpc
        self.sequence = 0
        self.historical = historical
        self.priority = 90 if historical else 10
        self.cache = {}

    async def call(self, method, params=None):
        limits = RPC_LIMITS.setdefault(self.http, {})
        gate = limits.setdefault(self.endpoint, EndpointBudget())
        if gate.cooldown > time.monotonic():
            raise RPCBackoff(gate.cooldown - time.monotonic())
        cacheable = (
            method
            in (
                "eth_getBlockByNumber",
                "eth_getTransactionReceipt",
                "eth_call",
                "getBlock",
                "getTransaction",
            )
            and params
            and not any(tag in str(params) for tag in ("latest", "pending"))
        )
        key = json.dumps([method, params], sort_keys=True)
        cached = self.cache.get(key)
        if cacheable and cached and cached[0] > time.monotonic():
            return cached[1]
        rate = self.chain.rpc_requests_per_second
        if self.historical and self.chain.historical_rpc_interval:
            rate = min(rate, 1 / self.chain.historical_rpc_interval)
        await gate.acquire(self.priority, rate)
        self.sequence += 1
        try:
            if gate.cooldown > time.monotonic():
                raise RPCBackoff(gate.cooldown - time.monotonic())
            r = await self.http.post(
                self.endpoint,
                json={
                    "jsonrpc": "2.0",
                    "id": self.sequence,
                    "method": method,
                    "params": params or [],
                },
            )
        finally:
            await gate.release()
        try:
            body = r.json()
        except ValueError:
            body = {}
        error = body.get("error") if isinstance(body, dict) else None
        message = str(error.get("message", "")).lower() if isinstance(error, dict) else ""
        if r.status_code == 429 or (
            error and ("rate limit" in message or "too many requests" in message)
        ):
            try:
                delay = max(60, float(r.headers.get("retry-after", "60")))
            except ValueError:
                delay = 60
            gate.cooldown = time.monotonic() + delay
            raise RPCBackoff(delay)
        # Persist safe categories, never provider messages containing URLs/keys.
        if error:
            if "archive" in message and ("token" in message or "key" in message):
                raise Unavailable(f"RPC {method}: archive access requires a provider token")
            if "historical state" in message or "missing trie" in message:
                raise Unavailable(f"RPC {method}: historical state unavailable")
            if "not allowed to access method" in message:
                raise Unavailable(f"RPC {method}: endpoint does not permit this method")
            if isinstance(error, dict) and error.get("code") == -32601:
                raise Unavailable(f"RPC {method}: method unsupported by endpoint")
        r.raise_for_status()
        if error:
            code = error.get("code") if isinstance(error, dict) else "unknown"
            raise Unavailable(f"RPC {method} failed (code {code})")
        result = body["result"]
        if cacheable and result is not None:
            if len(self.cache) >= 1000:
                self.cache.clear()
            self.cache[key] = (time.monotonic() + 2, result)
        return result

    async def decimals(self, token, block="latest"):
        return int(await self.call("eth_call", [{"to": token, "data": "0x313ce567"}, block]), 16)

    async def balance(self, token, wallet, block="latest"):
        return int(
            await self.call(
                "eth_call",
                [{"to": token, "data": "0x70a08231" + wallet.removeprefix("0x").zfill(64)}, block],
            ),
            16,
        )

    async def check_network(self):
        if self.chain.kind == "evm":
            actual = int(await self.call("eth_chainId"), 16)
            if actual != self.chain.fomo_id:
                raise Unavailable("RPC chain ID mismatch")
            return actual
        genesis = await self.call("getGenesisHash")
        if genesis != "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d":
            raise Unavailable("RPC is not Solana mainnet-beta")
        return genesis


class Monitor:
    """Wallet-specific token logs + finalized transaction deltas; durable overlap recovery."""

    def __init__(self, cfg: Settings, store: Store, http, wallets, emit, reorg, health):
        self.cfg, self.store, self.http = cfg, store, http
        self.wallets, self.emit, self.reorg, self.health = wallets, emit, reorg, health
        self.stop = asyncio.Event()
        self.started = now()
        self.wakes = {}

    def wake(self, chain):
        if chain in self.wakes:
            self.wakes[chain].set()

    def exit_traders(self, chain):
        with self.store.session() as session:
            return {
                p.trader
                for p in session.scalars(
                    select(Position).where(Position.chain == chain, Position.mode == self.cfg.mode)
                )
                if float(p.quantity) > 0
            }

    def ready(self, chain, trader, anchor=None, reason=None):
        key = f"monitor_ready:{chain}:{trader}"
        old = self.store.get(key, {})
        self.store.put(
            key,
            {
                "ready": reason is None,
                "at": now(),
                "anchor": old.get("anchor", anchor if anchor is not None else 0),
                "reason": reason,
            },
        )

    def new_activation(self, chain, trader):
        if self.cfg.selection_policy != "paper_research" or trader in self.exit_traders(chain):
            return False
        with self.store.session() as session:
            row = session.get(ResearchSelection, trader)
            epoch = (
                row.data.get("pair_activated", {}).get(chain)
                if row and row.data.get("selected")
                else None
            )
        key = f"monitor_epoch:{chain}:{trader}"
        if epoch is not None and self.store.get(key) != epoch:
            self.store.put(key, epoch)
            self.store.put(f"monitor_ready:{chain}:{trader}", {})
            for inventory_key in self.store.items(f"source_inventory:{chain}:{trader}:"):
                self.store.put(inventory_key, None)
            return True
        return False

    async def run_chain(self, chain: Chain):
        rpc = RPC(chain, self.http)
        wake = asyncio.Event()
        self.wakes[chain.name] = wake
        watcher = asyncio.create_task(self.wake_ws(chain, wake))
        try:
            network_ready = False
            while not self.stop.is_set():
                cycle_started = time.monotonic()
                try:
                    if not network_ready:
                        await rpc.check_network()
                        network_ready = True
                    wallets = self.wallets(chain)
                    if wallets:
                        if chain.kind == "evm":
                            await self.evm(rpc, wallets)
                        else:
                            await self.solana(rpc, wallets)
                    self.health(
                        "rpc:" + chain.name,
                        {
                            "state": "connected",
                            "at": now(),
                            "watching": len(wallets),
                            "poll_fallback_seconds": self.cfg.rpc_poll_seconds,
                        },
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    for trader in self.wallets(chain):
                        self.ready(
                            chain.name,
                            trader,
                            reason=str(exc) if isinstance(exc, Unavailable) else type(exc).__name__,
                        )
                    self.health(
                        "rpc:" + chain.name,
                        {
                            "state": "degraded",
                            "at": now(),
                            "error": str(exc)
                            if isinstance(exc, Unavailable)
                            else type(exc).__name__,
                        },
                    )
                try:
                    await asyncio.wait_for(wake.wait(), timeout=self.cfg.rpc_poll_seconds)
                except TimeoutError:
                    pass
                wake.clear()
                # A busy slot/head subscription cannot bypass the configured poll budget.
                remaining = self.cfg.rpc_poll_seconds - (time.monotonic() - cycle_started)
                if remaining > 0:
                    try:
                        await asyncio.wait_for(self.stop.wait(), remaining)
                    except TimeoutError:
                        pass
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

    async def wake_ws(self, chain, wake):
        if not chain.ws:
            return
        while not self.stop.is_set():
            try:
                async with websockets.connect(chain.ws, ping_interval=20, max_size=2**20) as ws:
                    if chain.kind == "evm":
                        await ws.send(
                            json.dumps(
                                {
                                    "jsonrpc": "2.0",
                                    "id": 1,
                                    "method": "eth_subscribe",
                                    "params": ["newHeads"],
                                }
                            )
                        )
                    else:
                        # Slot notifications trigger a wallet-specific finalized signature scan.
                        await ws.send(
                            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "slotSubscribe"})
                        )
                    async for raw in ws:
                        if json.loads(raw).get("params"):
                            wake.set()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.health("ws:" + chain.name, {"state": "polling fallback", "at": now()})
                try:
                    await asyncio.wait_for(self.stop.wait(), 5)
                except TimeoutError:
                    pass

    async def emit_movement(self, movement):
        sig = movement.signal
        previous = self.store.get(f"source_inventory:{sig.chain}:{sig.trader}:{sig.token}")
        if (
            previous
            and previous.get("quantity") is not None
            and sig.source_before is not None
            and not (self.cfg.selection_policy == "paper_research" and sig.chain == "solana")
            and previous["tx"] != sig.tx
            and previous["block"] <= sig.block
            and previous["quantity"] != str(sig.source_before)
        ):
            from .domain import D

            if D(previous["quantity"]) != sig.source_before:
                await self.reorg(sig.chain, sig.block)
        await self.emit(sig)
        self.store.put(
            f"source_inventory:{sig.chain}:{sig.trader}:{sig.token}",
            {
                "quantity": str(sig.source_after) if sig.source_after is not None else None,
                "tx": sig.tx,
                "block": sig.block,
                "block_hash": sig.block_hash,
                "at": sig.timestamp,
            },
        )
        self.store.put("last_chain_data", {"at": now(), "chain": sig.chain, "tx": sig.tx})
        if sig.side in ("buy", "sell"):
            self.store.put(
                f"research_activity:{sig.trader}:{sig.chain}",
                {"at": sig.timestamp, "source": "canonical RPC swap", "token": sig.token},
            )

    async def evm(self, rpc, wallets):
        head = int(await rpc.call("eth_blockNumber"), 16) - rpc.chain.confirmations
        groups = {}
        for trader, wallet in wallets.items():
            if self.new_activation(rpc.chain.name, trader):
                self.store.put(f"cursor:evm:{rpc.chain.name}:{trader}:{wallet.lower()}", None)
            cursor = (
                self.store.get(f"cursor:evm:{rpc.chain.name}:{trader}:{wallet.lower()}", {}) or {}
            )
            groups.setdefault((cursor.get("number"), cursor.get("hash")), {})[trader] = wallet
        exits = self.exit_traders(rpc.chain.name)
        ordered = sorted(groups.values(), key=lambda group: not bool(exits & group.keys()))
        for group in ordered:
            rpc.priority = 0 if exits & group.keys() else 10
            try:
                await self.evm_wallet(rpc, group, head)
            except Exception as exc:
                for uid in group:
                    self.ready(
                        rpc.chain.name,
                        uid,
                        reason=str(exc) if isinstance(exc, Unavailable) else type(exc).__name__,
                    )
                if isinstance(exc, RPCBackoff):
                    raise
                self.health(
                    "scan:" + rpc.chain.name,
                    {
                        "state": "degraded",
                        "at": now(),
                        "reason": str(exc) if isinstance(exc, Unavailable) else type(exc).__name__,
                    },
                )

    async def evm_wallet(self, rpc, wallets, head=None):
        chain = rpc.chain
        head = (
            int(await rpc.call("eth_blockNumber"), 16) - chain.confirmations
            if head is None
            else head
        )
        if head < 0:
            return
        trader, wallet = next(iter(wallets.items()))
        key = f"cursor:evm:{chain.name}:{trader}:{wallet.lower()}"
        cursor = self.store.get(key)

        def checkpoint(data):
            for uid, address in wallets.items():
                self.store.put(f"cursor:evm:{chain.name}:{uid}:{address.lower()}", data)

        # First observation starts here; no unbounded scanning of arbitrary chain history.
        if not cursor:
            block = await rpc.call("eth_getBlockByNumber", [hex(head), False])
            if not block or int(block.get("timestamp", "0x0"), 16) < now() - 120:
                raise Unavailable("confirmed RPC head is stale")
            checkpoint({"number": head, "hash": block["hash"]})
            for uid in wallets:
                self.ready(chain.name, uid, head)
            return
        old = await rpc.call("eth_getBlockByNumber", [hex(cursor["number"]), False])
        if not old or old["hash"] != cursor["hash"]:
            await self.reorg(chain.name, cursor["number"])
            # Pause affected entries; rescan overlap to rebuild activity without copying.
            rewind = max(0, cursor["number"] - 64)
            block = await rpc.call("eth_getBlockByNumber", [hex(rewind), False])
            checkpoint({"number": rewind, "hash": block["hash"]})
            for uid in wallets:
                self.ready(
                    chain.name, uid, reason="reorganized checkpoint; reconciliation required"
                )
            return
        if head - cursor["number"] > self.cfg.max_catchup_blocks:
            for uid in wallets:
                self.ready(chain.name, uid, reason="RPC gap exceeds configured backfill budget")
            self.health(
                "gap:" + chain.name,
                {"state": "blocked", "reason": "RPC gap exceeds configured backfill budget"},
            )
            return
        end = min(head, cursor["number"] + 50)
        if end <= cursor["number"]:
            if int(old.get("timestamp", "0x0"), 16) < now() - 120:
                raise Unavailable("confirmed RPC head is stale")
            for uid in wallets:
                self.ready(chain.name, uid, cursor["number"])
            return
        lo = cursor["number"] + 1
        # Fetch outgoing + incoming token logs; no generic trending-pool discovery.
        ids = [topic(w) for w in wallets.values()]
        query = {"fromBlock": hex(lo), "toBlock": hex(end)}
        outgoing = await rpc.call("eth_getLogs", [{**query, "topics": [TRANSFER, ids]}])
        incoming = await rpc.call("eth_getLogs", [{**query, "topics": [TRANSFER, None, ids]}])
        if (
            len(outgoing) >= 10000
            or len(incoming) >= 10000
            or any(log.get("removed") for log in outgoing + incoming)
        ):
            raise Unavailable("log scan saturated or reorganized")
        logs = {log["transactionHash"] + ":" + log["logIndex"]: log for log in outgoing + incoming}
        txs = sorted(
            {
                log["transactionHash"]: (
                    int(log["blockNumber"], 16),
                    int(log["transactionIndex"], 16),
                )
                for log in logs.values()
            }.items(),
            key=lambda pair: pair[1],
        )
        blocks = {}
        for uid in wallets:
            self.ready(chain.name, uid, cursor["number"])
        for tx, (height, _) in txs:
            if height not in blocks:
                blocks[height] = await rpc.call("eth_getBlockByNumber", [hex(height), False])
            block = blocks[height]
            receipt = await rpc.call("eth_getTransactionReceipt", [tx])
            if not receipt or receipt["blockHash"] != block["hash"]:
                raise Unavailable("unstable receipt; retry scan")
            if int(receipt["status"], 16) != 1:
                continue
            try:
                trace = await rpc.call("debug_traceTransaction", [tx, {"tracer": "callTracer"}])
            except (Unavailable, httpx.HTTPError):
                trace = None
            for trader, wallet in wallets.items():
                gaps = [] if self.cfg.selection_policy == "paper_research" else None
                for movement in await evm_movements(
                    rpc,
                    receipt,
                    block,
                    trader,
                    wallet,
                    list(logs.values()),
                    self.started,
                    trace,
                    gaps=gaps,
                ):
                    await self.emit_movement(movement)
                for gap in gaps or []:
                    from .activity import observe

                    observe(
                        self.store,
                        f"decode:{chain.name}:{trader}:{tx}:{gap['token']}",
                        kind="decode decision",
                        trader=trader,
                        chain=chain.name,
                        token=gap["token"],
                        status="blocked",
                        reason=gap["reason"],
                    )
                    with self.store.session() as session:
                        for p in session.scalars(
                            select(Position).where(
                                Position.chain == chain.name,
                                Position.trader == trader,
                                Position.token == gap["token"],
                                Position.mode == self.cfg.mode,
                            )
                        ):
                            p.needs_reconcile = True
        block = blocks.get(end) or await rpc.call("eth_getBlockByNumber", [hex(end), False])
        checkpoint({"number": end, "hash": block["hash"]})
        self.store.put("last_chain_data", {"at": now(), "chain": chain.name, "block": end})

    async def solana(self, rpc, wallets):
        if self.cfg.selection_policy == "paper_research":
            from .inventory import SolanaCurrent

            tracker = SolanaCurrent(self)
            for trader, wallet in wallets.items():
                rpc.priority = 0 if trader in self.exit_traders(rpc.chain.name) else 10
                try:
                    await tracker.scan(rpc, trader, wallet)
                except Exception as exc:
                    self.ready(
                        rpc.chain.name,
                        trader,
                        reason=str(exc) if isinstance(exc, Unavailable) else type(exc).__name__,
                    )
                    self.health(
                        f"inventory:{rpc.chain.name}:{trader}",
                        {
                            "state": "blocked",
                            "reason": str(exc)
                            if isinstance(exc, Unavailable)
                            else type(exc).__name__,
                            "at": now(),
                        },
                    )
            return
        for trader, wallet in wallets.items():
            key = f"cursor:solana:{wallet}"
            cursor = self.store.get(key)
            rows = []
            before = None
            reached = False
            for _ in range(10):
                params = {"limit": 100, "commitment": "finalized"}
                if before:
                    params["before"] = before
                page = await rpc.call("getSignaturesForAddress", [wallet, params])
                for row in page:
                    if cursor and row["signature"] == cursor["signature"]:
                        reached = True
                        break
                    rows.append(row)
                if reached or not page or not cursor:
                    break
                before = page[-1]["signature"]
            if cursor and not reached:
                self.health(
                    "gap:solana:" + trader,
                    {"state": "blocked", "reason": "signature cursor not reached"},
                )
                continue
            if not cursor:
                if rows:
                    self.store.put(key, {"signature": rows[0]["signature"]})
                continue
            for row in reversed(rows):
                if row.get("err"):
                    continue
                tx = await rpc.call(
                    "getTransaction",
                    [
                        row["signature"],
                        {
                            "encoding": "jsonParsed",
                            "commitment": "finalized",
                            "maxSupportedTransactionVersion": SOLANA_READ_VERSION,
                        },
                    ],
                )
                if not tx:
                    raise Unavailable("finalized transaction unavailable")
                for movement in solana_movements(
                    tx, trader, wallet, row["signature"], rpc.chain, self.started
                ):
                    await self.emit_movement(movement)
            if rows:
                self.store.put(key, {"signature": rows[0]["signature"]})


def solana_signals(tx, trader, wallet, signature, chain, started):
    return [m.signal for m in solana_movements(tx, trader, wallet, signature, chain, started)]
