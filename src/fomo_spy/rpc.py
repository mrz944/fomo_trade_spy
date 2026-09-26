from __future__ import annotations

import asyncio
import json
from collections import defaultdict

import httpx
import websockets

from .config import Chain, Settings
from .db import Store
from .domain import D, Signal, now
from .providers import Unavailable

TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def topic(wallet):
    return "0x" + wallet.lower().removeprefix("0x").zfill(64)


def log_delta(log, wallet):
    if len(log.get("topics", [])) != 3 or log["topics"][0].lower() != TRANSFER:
        return 0
    value = int(log["data"], 16)
    return value * (
        (log["topics"][2].lower() == topic(wallet)) - (log["topics"][1].lower() == topic(wallet))
    )


class RPC:
    def __init__(self, chain: Chain, http: httpx.AsyncClient):
        self.chain, self.http = chain, http
        self.sequence = 0

    async def call(self, method, params=None):
        self.sequence += 1
        r = await self.http.post(
            self.chain.rpc,
            json={"jsonrpc": "2.0", "id": self.sequence, "method": method, "params": params or []},
        )
        r.raise_for_status()
        body = r.json()
        if body.get("error"):
            raise Unavailable(f"RPC {method} failed (code {body['error'].get('code')})")
        return body["result"]

    async def decimals(self, token):
        return int(await self.call("eth_call", [{"to": token, "data": "0x313ce567"}, "latest"]), 16)

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

    async def evm(self, rpc, wallets):
        chain = rpc.chain
        head = int(await rpc.call("eth_blockNumber"), 16) - chain.confirmations
        if head < 0:
            return
        key = "cursor:evm:" + chain.name
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
            for trader, wallet in wallets.items():
                deltas = defaultdict(int)
                for log in receipt["logs"]:
                    delta = log_delta(log, wallet)
                    if delta:
                        deltas[log["address"].lower()] += delta
                settlement_delta = deltas.pop(chain.settlement.lower(), 0)
                tokens = [(t, d) for t, d in deltas.items() if d]
                # Multi-token bundles and native-only routes require a trace decoder; don't guess.
                for token, delta in tokens:
                    decimals = await rpc.decimals(token)
                    before = await rpc.balance(token, wallet, hex(height - 1))
                    for log in logs.values():
                        if (
                            int(log["blockNumber"], 16) == height
                            and int(log["transactionIndex"], 16)
                            < int(receipt["transactionIndex"], 16)
                            and log["address"].lower() == token
                        ):
                            before += log_delta(log, wallet)
                    side = (
                        ("buy" if delta > 0 else "sell")
                        if len(tokens) == 1 and delta * settlement_delta < 0
                        else "transfer"
                    )
                    ts = int(block["timestamp"], 16)
                    signal = Signal(
                        trader=trader,
                        chain=chain.name,
                        token=token,
                        side=side,
                        quantity=D(abs(delta)) / 10**decimals,
                        source_before=D(before) / 10**decimals,
                        source_after=D(before + delta) / 10**decimals,
                        token_decimals=decimals,
                        tx=tx,
                        block=height,
                        block_hash=block["hash"],
                        timestamp=ts,
                        historical=ts < self.started,
                        finalized=True,
                    )
                    await self.emit(signal)
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
                for sig in solana_signals(
                    tx, trader, wallet, row["signature"], rpc.chain, self.started
                ):
                    await self.emit(sig)
            if rows:
                self.store.put(key, {"signature": rows[0]["signature"]})


def solana_signals(tx, trader, wallet, signature, chain, started):
    meta = tx["meta"]
    if meta.get("err"):
        return []
    pre, post, decimals = defaultdict(int), defaultdict(int), {}
    for field, values in [("preTokenBalances", pre), ("postTokenBalances", post)]:
        for row in meta.get(field, []):
            if row.get("owner") == wallet:
                token = row["mint"]
                values[token] += int(row["uiTokenAmount"]["amount"])
                decimals[token] = row["uiTokenAmount"]["decimals"]
    deltas = {t: post[t] - pre[t] for t in pre.keys() | post.keys() if post[t] != pre[t]}
    settlement = deltas.pop(chain.settlement, 0)
    signals = []
    for token, delta in deltas.items():
        side = (
            ("buy" if delta > 0 else "sell")
            if len(deltas) == 1 and delta * settlement < 0
            else "transfer"
        )
        timestamp = tx.get("blockTime")
        if timestamp is None:
            continue
        signals.append(
            Signal(
                trader=trader,
                chain=chain.name,
                token=token,
                side=side,
                quantity=D(abs(delta)) / 10 ** decimals[token],
                source_before=D(pre[token]) / 10 ** decimals[token],
                source_after=D(post[token]) / 10 ** decimals[token],
                token_decimals=decimals[token],
                tx=signature,
                block=tx["slot"],
                block_hash=str(tx["slot"]) + ":finalized",
                timestamp=timestamp,
                historical=timestamp < started,
                finalized=True,
            )
        )
    return signals
