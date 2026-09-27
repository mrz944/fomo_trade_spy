import copy
import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select

from fomo_spy.config import default_chains
from fomo_spy.db import Cash, EvidenceFill, Ledger, ProviderSwap, Store
from fomo_spy.domain import D, now
from fomo_spy.engine import Engine
from fomo_spy.history import History, Prices, ingest_provider
from fomo_spy.normalization import (
    TRANSFER,
    evm_movements,
    native_trace_delta,
    solana_movements,
    topic,
)
from fomo_spy.providers import Fomo, Relay
from fomo_spy.rpc import RPC

FIXTURES = Path(__file__).parent / "fixtures"
CAPTURE = json.loads((FIXTURES / "fomo-captured.json").read_text())
BASE = json.loads((FIXTURES / "base-captured.json").read_text())
WALLET = "0x" + "1" * 40
ROUTER = "0x" + "2" * 40


def test_captured_provider_rows_are_discovery_only_and_deduplicate(cfg):
    store = Store(cfg.state_dir / "spy.sqlite")
    uid = CAPTURE["trader"]["userId"]
    page = CAPTURE["page"]
    for _ in range(2):
        assert ingest_provider(store, uid, {"pages": [page, page]}) == len(page["swaps"])
    with store.session() as s:
        assert s.scalar(select(func.count()).select_from(ProviderSwap)) == len(page["swaps"])
        assert s.scalar(select(func.count()).select_from(EvidenceFill)) == 0
        row = s.scalars(select(ProviderSwap)).first()
        assert row.data["legs"][0]["fee_usd"] is None
    store.close()


@pytest.mark.parametrize("cap", [False, True])
async def test_provider_repeat_and_cap_stop_billing(cfg, monkeypatch, cap):
    monkeypatch.setenv("FOMO_API_KEY", "fixture-not-a-credential")
    store = Store(cfg.state_dir / "spy.sqlite")
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(
            200, json={**CAPTURE["page"], "nextCursor": str(len(calls)), "sourceCapped": cap}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await Fomo(cfg, store, http).history(CAPTURE["trader"]["userId"])
    assert len(calls) == (1 if cap else 2)
    assert not result["complete_30d"]
    store.close()


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"price": 1, "timestamp": 1, "confidence": 1},
        {"price": 1, "timestamp": 100000, "confidence": 0.1},
    ],
)
async def test_prices_never_substitute_current_or_unknown(cfg, payload):
    store = Store(cfg.state_dir / "spy.sqlite")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json={"coins": {"base:coin": payload}})
        )
    ) as http:
        assert await Prices(store, http).get("base:coin", 100000) is None
    store.close()


def test_native_trace_excludes_reverts_and_delegate_value():
    trace = {
        "type": "CALL",
        "from": WALLET,
        "to": ROUTER,
        "value": hex(100),
        "calls": [
            {"type": "DELEGATECALL", "from": WALLET, "to": ROUTER, "value": hex(100)},
            {"type": "CALL", "from": ROUTER, "to": WALLET, "value": hex(5)},
            {"type": "CALL", "from": ROUTER, "to": WALLET, "value": hex(90), "error": "reverted"},
        ],
    }
    assert native_trace_delta(trace, WALLET) == -95
    assert isinstance(native_trace_delta(BASE["trace"], BASE["receipt"]["from"]), int)


def test_solana_native_swap_rent_and_fee_are_separate():
    c = default_chains()[0]
    tx = {
        "slot": 1,
        "blockTime": int(now()),
        "transaction": {"message": {"accountKeys": [{"pubkey": "wallet"}, {"pubkey": "ata"}]}},
        "meta": {
            "err": None,
            "fee": 5000,
            "preBalances": [10**9, 0],
            "postBalances": [10**9 - 10**8 - 2039280 - 5000, 2039280],
            "preTokenBalances": [],
            "postTokenBalances": [
                {
                    "owner": "wallet",
                    "mint": "token",
                    "accountIndex": 1,
                    "uiTokenAmount": {"amount": "1000000", "decimals": 6},
                }
            ],
        },
    }
    (movement,) = solana_movements(tx, "u", "wallet", "sig", c, 0)
    assert movement.signal.side == "buy"
    assert movement.consideration == D(".1")
    assert movement.gas_native == D(".000005")
    del tx["meta"]["fee"]
    (movement,) = solana_movements(tx, "u", "wallet", "sig", c, 0)
    assert movement.signal.side == "transfer"
    assert movement.gas_native is None


class ChainFixture:
    """Synthetic scenario derived from captured RPC shapes, never real eligibility."""

    def __init__(self, cfg, at):
        self.cfg, self.at = cfg, at
        self.chain = cfg.chain("base")
        self.receipts = {}
        self.traces = {}
        self.tokens = ["0x" + str(i) * 40 for i in range(3, 8)]
        for i in range(20):
            for side, block in (("buy", 300 + i * 24), ("sell", 301 + i * 24)):
                self.add(side, block, self.tokens[i % 5])

    def timestamp(self, height):
        return int(self.at - (1000 - height) * 3600)

    def add(self, side, block, token, quantity=100000000, native=False):
        txid = "0x" + f"{block:064x}"
        buy = side == "buy"
        receipt = copy.deepcopy(BASE["receipt"])
        receipt.update(
            transactionHash=txid,
            blockHash=f"block{block}",
            blockNumber=hex(block),
            transactionIndex="0x0",
            status="0x1",
            gasUsed=hex(21000),
            effectiveGasPrice=hex(10**9),
            l1Fee="0x0",
            logs=[],
        )
        receipt["from"] = WALLET
        amounts = [(token, quantity if buy else -quantity)]
        if not native:
            amounts.append((self.chain.settlement, -25000000 if buy else 35000000))
        for i, (address, delta) in enumerate(amounts):
            receipt["logs"].append(
                {
                    "address": address,
                    "topics": [
                        TRANSFER,
                        topic(ROUTER if delta > 0 else WALLET),
                        topic(WALLET if delta > 0 else ROUTER),
                    ],
                    "data": hex(abs(delta)),
                    "blockNumber": hex(block),
                    "blockHash": f"block{block}",
                    "transactionIndex": "0x0",
                    "transactionHash": txid,
                    "logIndex": hex(i),
                }
            )
        self.receipts[txid] = receipt
        self.traces[txid] = {
            "type": "CALL",
            "from": WALLET if buy else ROUTER,
            "to": ROUTER if buy else WALLET,
            "value": hex(10**16 if native else 0),
        }
        return receipt

    def balance(self, token, block):
        total = 0
        for receipt in self.receipts.values():
            if int(receipt["blockNumber"], 16) <= block:
                for log in receipt["logs"]:
                    if log["address"] == token:
                        total += int(log["data"], 16) * (
                            1 if log["topics"][2] == topic(WALLET) else -1
                        )
        return total

    def rpc(self, method, params):
        if method == "eth_chainId":
            return hex(8453)
        if method == "eth_blockNumber":
            return hex(1002)
        if method == "eth_getBalance":
            return hex(10**18)
        if method == "eth_getBlockByNumber":
            height = int(params[0], 16)
            return {
                "hash": f"block{height}",
                "timestamp": hex(self.timestamp(height)),
                "transactions": [next(iter(self.receipts))],
            }
        if method == "eth_call":
            if params[0]["data"] == "0x313ce567":
                return hex(6)
            return hex(max(0, self.balance(params[0]["to"], int(params[1], 16))))
        if method == "eth_getLogs":
            lo, hi = int(params[0]["fromBlock"], 16), int(params[0]["toBlock"], 16)
            return [
                log
                for r in self.receipts.values()
                for log in r["logs"]
                if lo <= int(log["blockNumber"], 16) <= hi
            ]
        if method == "trace_filter":
            return []
        if method == "eth_getTransactionReceipt":
            return self.receipts[params[0]]
        if method == "debug_traceTransaction":
            return self.traces[params[0]]
        raise AssertionError(method)

    def handler(self, req):
        if req.url.host == "coins.llama.fi":
            ts, asset = req.url.path.split("/")[-2:]
            return httpx.Response(
                200,
                json={
                    "coins": {
                        asset: {
                            "timestamp": int(ts),
                            "price": 2000 if asset.startswith("coingecko") else 1,
                            "confidence": 1,
                        }
                    }
                },
            )
        if req.url.path == "/quote/v2":
            body = json.loads(req.content)
            amount = int(body["amount"])
            buy = body["originCurrency"] == self.chain.settlement
            out = amount * 4 if buy else amount // 4

            def currency(address, amount):
                return {
                    "currency": {"chainId": 8453, "address": address, "decimals": 6},
                    "amount": str(amount),
                    "minimumAmount": str(amount * 99 // 100),
                    "amountUsd": str(D(amount) / 10**6),
                }

            return httpx.Response(
                200,
                json={
                    "steps": [{"kind": "transaction"}],
                    "details": {
                        "sender": body["user"],
                        "recipient": body["recipient"],
                        "currencyIn": currency(body["originCurrency"], amount),
                        "currencyOut": currency(body["destinationCurrency"], out),
                    },
                    "fees": {"gas": {"amountUsd": ".01"}},
                },
            )
        if req.method == "GET":
            return httpx.Response(200, json=CAPTURE["page"])
        body = json.loads(req.content)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": self.rpc(body["method"], body["params"]),
            },
        )


async def test_production_pipeline_ranking_quote_buy_partial_full_exit_restart(cfg, monkeypatch):
    monkeypatch.setenv("FOMO_API_KEY", "fixture-only")
    store = Store(cfg.state_dir / "spy.sqlite")
    scenario = ChainFixture(cfg, now())
    trader = {**CAPTURE["trader"], "wallets": {"evm": WALLET}}
    uid = trader["userId"]
    async with httpx.AsyncClient(transport=httpx.MockTransport(scenario.handler)) as http:
        raw = await Fomo(cfg, store, http).history(uid)
        store.put("history_raw:" + uid, raw)
        history = History(cfg, store, http, lambda *_: None)
        result = await history.step(trader)
        assert result["eligible"], result
        assert result["completed"] == 20
        assert result["state"] == "evaluation complete"
        engine = Engine(cfg, store, Relay(cfg, http))
        assert engine.selected() == [uid]
        assert engine.snapshot()["orders"] == []  # Reconstruction never emits trade signals.
        rpc = RPC(cfg.chain("base"), http)
        token = scenario.tokens[0]
        for i, (side, quantity) in enumerate(
            (("buy", 100000000), ("sell", 25000000), ("sell", 75000000))
        ):
            receipt = scenario.add(side, 1003 + i, token, quantity, native=True)
            block = {"hash": receipt["blockHash"], "timestamp": hex(int(now()) + 1)}
            movements = await evm_movements(
                rpc,
                receipt,
                block,
                uid,
                WALLET,
                receipt["logs"],
                engine.started,
                scenario.traces[receipt["transactionHash"]],
            )
            sig = movements[0].signal
            await engine.event(sig)
            assert await engine.event(sig) == "duplicate"
            positions = engine.snapshot()["positions"]
            if i == 0:
                bought = D(positions[0]["quantity"])
            elif i == 1:
                assert D(positions[0]["quantity"]) == bought * D(".75")
            else:
                assert positions == []
        assert len(engine.snapshot()["orders"]) == 3
        with store.session() as s:
            ledger = list(s.scalars(select(Ledger)))
            cash = D(s.get(Cash, "paper:base").balance)
            assert len(ledger) == 3
            assert cash == cfg.paper.capital_usd + sum((D(r.cash_delta) for r in ledger), D(0))
            assert cash == cfg.paper.capital_usd + sum((D(r.realized) for r in ledger), D(0))
    store.close()
    reopened = Store(cfg.state_dir / "spy.sqlite")
    with reopened.session() as s:
        assert D(s.get(Cash, "paper:base").balance) == cash
        assert s.scalar(select(func.count()).select_from(EvidenceFill)) == 40
    reopened.close()


async def test_missing_capability_is_not_unprofitable_evaluation(cfg):
    store = Store(cfg.state_dir / "spy.sqlite")
    scenario = ChainFixture(cfg, now())

    def handler(req):
        if req.method == "POST" and json.loads(req.content)["method"] == "trace_filter":
            return httpx.Response(200, json={"error": {"code": -32601}})
        return scenario.handler(req)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await History(cfg, store, http, lambda *_: None).step(
            {"userId": "u", "wallets": {"evm": WALLET}}
        )
    assert result["state"] == "data unavailable"
    assert result["reasons"] == ["30-day chain evidence incomplete"]
    assert "trace_filter" in result["missing"][0]
    assert not result["eligible"]
    store.close()


async def test_missing_fee_remains_unknown(cfg):
    store = Store(cfg.state_dir / "spy.sqlite")
    scenario = ChainFixture(cfg, now())
    receipt = next(iter(scenario.receipts.values()))
    del receipt["l1Fee"]
    async with httpx.AsyncClient(transport=httpx.MockTransport(scenario.handler)) as http:
        rpc = RPC(cfg.chain("base"), http)
        block = scenario.rpc("eth_getBlockByNumber", [receipt["blockNumber"]])
        (movement,) = await evm_movements(
            rpc,
            receipt,
            block,
            "u",
            WALLET,
            receipt["logs"],
            float("inf"),
            scenario.traces[receipt["transactionHash"]],
        )
        assert movement.gas_native is None
        await History(cfg, store, http, lambda *_: None).persist(movement)
        with store.session() as s:
            assert s.get(EvidenceFill, movement.signal.key).data["fill"]["fee_usd"] is None
    store.close()


async def test_pre_window_cost_is_reconstructed_before_eligibility(cfg):
    from fomo_spy.db import ScanCheckpoint
    from fomo_spy.history import read

    store = Store(cfg.state_dir / "spy.sqlite")
    scenario = ChainFixture(cfg, now())
    original = next(iter(scenario.receipts))
    del scenario.receipts[original]
    scenario.add("buy", 200, scenario.tokens[0])
    trader = {"userId": "u", "wallets": {"evm": WALLET}}
    async with httpx.AsyncClient(transport=httpx.MockTransport(scenario.handler)) as http:
        history = History(cfg, store, http, lambda *_: None)
        first = await history.step(trader)
        assert not first["eligible"]
        checkpoint = read(store, ScanCheckpoint, f"u:base:{WALLET}")
        assert checkpoint["backfill_end"] < 300
        second = await history.step(trader)
        assert second["eligible"], second
        assert second["completed"] == 20
    store.close()


async def test_scan_resume_and_reorg_invalidate_coverage(cfg):
    cfg.history_batch_blocks = 100
    path = cfg.state_dir / "spy.sqlite"
    store = Store(path)
    scenario = ChainFixture(cfg, now())
    trader = {"userId": "u", "wallets": {"evm": WALLET}}
    async with httpx.AsyncClient(transport=httpx.MockTransport(scenario.handler)) as http:
        history = History(cfg, store, http, lambda *_: None)
        assert not (await history.step(trader))["eligible"]
        store.close()
        store = Store(path)
        history = History(cfg, store, http, lambda *_: None)
        for _ in range(10):
            result = await history.step(trader)
            if result["eligible"]:
                break
        assert result["eligible"]
        original_rpc = scenario.rpc

        def reorg(method, params):
            value = original_rpc(method, params)
            if method == "eth_getBlockByNumber" and params[0] == hex(1000):
                value["hash"] = "changed"
            return value

        scenario.rpc = reorg
        result = await history.step(trader)
        assert not result["eligible"]
        assert "reorganized" in result["missing"][0]
    store.close()


async def test_wrap_deposit_is_not_double_counted_as_native_spend(cfg):
    from fomo_spy.normalization import DEPOSIT, WRAPPED

    scenario = ChainFixture(cfg, now())
    receipt = scenario.add("buy", 1003, scenario.tokens[0], native=True)
    wrapped = WRAPPED["base"]
    receipt["logs"].extend(
        [
            {"address": wrapped, "topics": [DEPOSIT, topic(WALLET)], "data": hex(10**16)},
            {
                "address": wrapped,
                "topics": [TRANSFER, topic(WALLET), topic(ROUTER)],
                "data": hex(10**16),
            },
        ]
    )
    # Extra logs have the same transaction identity as the real token leg.
    for log in receipt["logs"]:
        log.update(blockNumber=hex(1003), transactionIndex="0x0")
    async with httpx.AsyncClient(transport=httpx.MockTransport(scenario.handler)) as http:
        movements = await evm_movements(
            RPC(cfg.chain("base"), http),
            receipt,
            {"hash": "block1003", "timestamp": hex(int(now()))},
            "u",
            WALLET,
            receipt["logs"],
            0,
            scenario.traces[receipt["transactionHash"]],
        )
    assert len(movements) == 1
    assert movements[0].consideration == D(".01")
