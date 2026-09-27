from __future__ import annotations

import asyncio
import json

import httpx
import websockets

from .config import Chain, Settings
from .db import Store
from .domain import now
from .normalization import TRANSFER, evm_movements, solana_movements, topic
from .normalization import log_delta as log_delta
from .providers import Unavailable


class RPC:
    def __init__(self, chain: Chain, http: httpx.AsyncClient, *, historical=False):
        self.chain, self.http = chain, http
        self.endpoint = (chain.historical_rpc or chain.rpc) if historical else chain.rpc
        self.sequence = 0

    async def call(self, method, params=None):
        self.sequence += 1
        r = await self.http.post(
            self.endpoint,
            json={"jsonrpc": "2.0", "id": self.sequence, "method": method, "params": params or []},
        )
        r.raise_for_status()
        body = r.json()
        if body.get("error"):
            raise Unavailable(f"RPC {method} failed (code {body['error'].get('code')})")
        return body["result"]

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

    async def run_chain(self, chain: Chain):
        rpc = RPC(chain, self.http)
        wake = asyncio.Event()
        watcher = asyncio.create_task(self.wake_ws(chain, wake))
        try:
            await rpc.check_network()
            while not self.stop.is_set():
                try:
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
                    self.health(
                        "rpc:" + chain.name,
                        {"state": "degraded", "at": now(), "error": type(exc).__name__},
                    )
                try:
                    await asyncio.wait_for(wake.wait(), timeout=self.cfg.rpc_poll_seconds)
                except TimeoutError:
                    pass
                wake.clear()
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
                "quantity": str(sig.source_after),
                "tx": sig.tx,
                "block": sig.block,
                "block_hash": sig.block_hash,
                "at": sig.timestamp,
            },
        )
        self.store.put("last_chain_data", {"at": now(), "chain": sig.chain, "tx": sig.tx})

    async def evm(self, rpc, wallets):
        for trader, wallet in wallets.items():
            await self.evm_wallet(rpc, {trader: wallet})

    async def evm_wallet(self, rpc, wallets):
        chain = rpc.chain
        head = int(await rpc.call("eth_blockNumber"), 16) - chain.confirmations
        if head < 0:
            return
        trader, wallet = next(iter(wallets.items()))
        key = f"cursor:evm:{chain.name}:{trader}:{wallet.lower()}"
        cursor = self.store.get(key)
        # First observation starts here; no unbounded scanning of arbitrary chain history.
        if not cursor:
            block = await rpc.call("eth_getBlockByNumber", [hex(head), False])
            self.store.put(key, {"number": head, "hash": block["hash"]})
            return
        old = await rpc.call("eth_getBlockByNumber", [hex(cursor["number"]), False])
        if not old or old["hash"] != cursor["hash"]:
            await self.reorg(chain.name, cursor["number"])
            # Pause affected entries; rescan overlap to rebuild activity without copying.
            rewind = max(0, cursor["number"] - 64)
            block = await rpc.call("eth_getBlockByNumber", [hex(rewind), False])
            self.store.put(key, {"number": rewind, "hash": block["hash"]})
            return
        if head - cursor["number"] > self.cfg.max_catchup_blocks:
            self.health(
                "gap:" + chain.name,
                {"state": "blocked", "reason": "RPC gap exceeds configured backfill budget"},
            )
            return
        end = min(head, cursor["number"] + 50)
        if end <= cursor["number"]:
            return
        lo = cursor["number"] + 1
        # Fetch outgoing + incoming token logs; no generic trending-pool discovery.
        ids = [topic(w) for w in wallets.values()]
        query = {"fromBlock": hex(lo), "toBlock": hex(end)}
        outgoing = await rpc.call("eth_getLogs", [{**query, "topics": [TRANSFER, ids]}])
        incoming = await rpc.call("eth_getLogs", [{**query, "topics": [TRANSFER, None, ids]}])
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
                for movement in await evm_movements(
                    rpc, receipt, block, trader, wallet, list(logs.values()), self.started, trace
                ):
                    await self.emit_movement(movement)
        block = blocks.get(end) or await rpc.call("eth_getBlockByNumber", [hex(end), False])
        self.store.put(key, {"number": end, "hash": block["hash"]})

    async def solana(self, rpc, wallets):
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
                            "maxSupportedTransactionVersion": 0,
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
