"""Isolated scenario using production discovery, admission, RPC, quotes and accounting."""

import asyncio

import httpx
from sqlalchemy import select
from test_history import CAPTURE, WALLET, ChainFixture

from fomo_spy.current import probe_current
from fomo_spy.db import Ledger, Store
from fomo_spy.domain import D, now
from fomo_spy.engine import Engine
from fomo_spy.providers import Fomo, Relay
from fomo_spy.rpc import RPC, Monitor
from fomo_spy.scheduler import EndpointBudget


async def test_current_production_pipeline_without_historical_qualification(cfg, monkeypatch):
    monkeypatch.setenv("FOMO_API_KEY", "isolated-fixture-not-a-secret")
    cfg.selection_policy = "paper_research"
    store = Store(cfg.state_dir / "spy.sqlite")
    scenario = ChainFixture(cfg, now())
    scenario.head = 1000
    original = scenario.rpc
    trader = {**CAPTURE["trader"], "wallets": {"evm": WALLET}}
    uid = trader["userId"]

    def rpc_result(method, params):
        if method == "eth_blockNumber":
            return hex(scenario.head + cfg.chain("base").confirmations)
        result = original(method, params)
        if method == "eth_getBlockByNumber":
            height = int(params[0], 16)
            result["timestamp"] = hex(int(now()) + 1)
            result["transactions"] = [
                tx for tx, r in scenario.receipts.items() if int(r["blockNumber"], 16) == height
            ]
        return result

    scenario.rpc = rpc_result

    def handler(request):
        if request.url.path == "/v2/leaderboard/30d":
            return httpx.Response(200, json={"traders": [trader]})
        return scenario.handler(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        fomo = Fomo(cfg, store, http)
        leaders = await fomo.discover()
        received = fomo.discovery_received_at
        assert await fomo.discover() == leaders
        assert fomo.discovery_received_at == received  # Restart/cache cannot renew evidence age.
        store.put("current_leaderboard", {"traders": [uid], "expires": received + 21600})
        store.put("trader:" + uid, leaders[0])
        quotes = Relay(cfg, http)
        report = await probe_current(cfg.chain("base"), WALLET, http, quotes, {8453: {}})
        # Empty catalog entries are rejected; actual catalog records have a chain ID.
        assert not report["observe_ready"]
        report = await probe_current(cfg.chain("base"), WALLET, http, quotes, {8453: {"id": 8453}})
        assert report["observe_ready"], report
        store.put("current_capability:base", report)
        engine = Engine(cfg, store, quotes)
        engine.research.refresh(at=now() - 1)
        assert engine.selected() == [uid]
        assert engine.rankings() == []  # No manufactured verified eligibility.
        monitor = Monitor(cfg, store, http, lambda _: {}, engine.event, engine.reorg, engine.health)
        rpc = RPC(cfg.chain("base"), http)
        await monitor.evm(rpc, {uid: WALLET})
        assert engine.snapshot()["orders"] == []
        for side, quantity in (("buy", 100000000), ("sell", 25000000), ("sell", 75000000)):
            scenario.head += 1
            scenario.add(side, scenario.head, scenario.tokens[0], quantity, native=True)
            await monitor.evm(rpc, {uid: WALLET})
            positions = engine.snapshot()["positions"]
            if side == "buy":
                assert positions, engine.snapshot()["activity"]
                bought = D(positions[0]["quantity"])
                # Exits continue after exclusion and pause, through the same monitor.
                store.put("excluded", [uid])
                store.put("paused:paper", True)
                assert engine.watched("base") == [uid]
            elif quantity == 25000000:
                assert D(positions[0]["quantity"]) == bought * D(".75")
            else:
                assert positions == []
        await monitor.evm(rpc, {uid: WALLET})
        assert len(engine.snapshot()["orders"]) == 3
        with store.session() as session:
            ledger = list(session.scalars(select(Ledger)))
            assert len(ledger) == 3
            assert all(r.details["selection_policy"] == "paper_research" for r in ledger)
            cash = D(engine.snapshot()["cash"]["base"])
            assert cash == cfg.paper.capital_usd + sum(D(r.cash_delta) for r in ledger)
            assert cash == cfg.paper.capital_usd + sum(D(r.realized) for r in ledger)
        store.close()
        store = Store(cfg.state_dir / "spy.sqlite")
        restored = Engine(cfg, store, quotes)
        assert D(restored.snapshot()["cash"]["base"]) == cash
        assert restored.snapshot()["policy_performance"]["paper_research"]["fees_usd"]
    store.close()


async def test_rpc_budget_prioritizes_exits_and_cleans_cancelled_waiter():
    budget = EndpointBudget()
    await budget.acquire(90, 100)
    await budget.acquire(90, 100)
    order = []

    async def work(priority):
        await budget.acquire(priority, 100)
        order.append(priority)
        await budget.release()

    history = asyncio.create_task(work(90))
    cancelled = asyncio.create_task(work(50))
    exit_scan = asyncio.create_task(work(0))
    await asyncio.sleep(0)
    cancelled.cancel()
    await asyncio.gather(cancelled, return_exceptions=True)
    await budget.release()
    await budget.release()
    await asyncio.gather(history, exit_scan)
    assert order == [0, 90]
    assert budget.inflight == 0 and budget.queue == []
