import asyncio

from conftest import signal
from sqlalchemy import select

from fomo_spy.db import Cash, Event, Ledger, Order, Position
from fomo_spy.domain import D, now
from fomo_spy.engine import Engine


async def test_dedup_across_rpc_social_and_restart(engine):
    sig = signal()
    assert await engine.event(sig) == "processed"
    assert await engine.event(sig.model_copy(update={"provider": "social"})) == "duplicate"
    replacement = Engine(engine.cfg, engine.store, engine.quotes)
    assert await replacement.event(sig) == "duplicate"
    with engine.store.session() as s:
        assert len(s.scalars(select(Order)).all()) == 1
        assert len(s.scalars(select(Ledger)).all()) == 1
        assert D(s.get(Cash, "paper:base").balance) == D("974.95")


async def test_concurrent_duplicate_is_one_order(engine):
    sig = signal()
    assert sorted(await asyncio.gather(engine.event(sig), engine.event(sig))) == [
        "duplicate",
        "processed",
    ]


async def test_history_and_stale_entries_never_copy(engine):
    assert "historical" in await engine.event(signal(historical=True))
    assert "historical" in await engine.event(signal(tx="old", timestamp=now() - 40))
    engine.started = now() - 1000
    assert "older than 30" in await engine.event(signal(tx="stale", timestamp=now() - 40))
    assert not engine.snapshot()["orders"]


async def test_proportional_partial_exits_and_source_isolation(engine):
    await engine.event(signal())
    await engine.event(signal(trader="demo-6", tx="other"))
    before = {p["trader"]: p for p in engine.snapshot()["positions"]}
    assert (
        await engine.event(
            signal(side="sell", tx="sell", quantity=D(25), source_before=D(100), source_after=D(75))
        )
        == "processed"
    )
    after = {p["trader"]: p for p in engine.snapshot()["positions"]}
    sold = D(before["demo-7"]["quantity"]) * D(".25")
    assert abs(D(after["demo-7"]["quantity"]) - (D(before["demo-7"]["quantity"]) - sold)) < D(
        ".000001"
    )
    assert after["demo-6"]["quantity"] == before["demo-6"]["quantity"]
    assert D(after["demo-7"]["cost"]) < D(before["demo-7"]["cost"])


async def test_unknown_inventory_requires_reconciliation(engine):
    await engine.event(signal())
    reason = await engine.event(
        signal(side="sell", tx="sell", quantity=D(10), source_before=None, source_after=None)
    )
    assert "unknown" in reason
    assert engine.snapshot()["positions"][0]["needs_reconcile"]


async def test_excluded_and_deselected_sources_remain_watched(engine):
    await engine.event(signal())
    engine.store.put("excluded", ["demo-7"])
    assert "demo-7" not in engine.selected()
    assert "demo-7" in engine.watched()
    assert (
        await engine.event(
            signal(side="sell", tx="exit", quantity=D(100), source_before=D(100), source_after=D(0))
        )
        == "processed"
    )
    assert "demo-7" not in engine.watched()


async def test_pause_does_not_block_exits(engine):
    await engine.event(signal())
    engine.store.put("paused:paper", True)
    assert "paused" in await engine.event(signal(tx="new"))
    assert (
        await engine.event(signal(side="sell", tx="exit", source_before=D(100), source_after=D(0)))
        == "processed"
    )


async def test_reorg_halts_chain_and_preserves_ledger(engine):
    sig = signal()
    await engine.event(sig)
    await engine.event(sig.model_copy(update={"block_hash": "replacement"}))
    assert engine.store.get("chain_halt:base")
    assert engine.snapshot()["positions"][0]["needs_reconcile"]
    with engine.store.session() as s:
        assert s.get(Event, sig.key).status == "reorg"
        assert len(s.scalars(select(Ledger)).all()) == 1


async def test_risk_exposure_position_and_daily_loss_halts(engine):
    engine.cfg.paper.exposure_usd = D(26)
    await engine.event(signal())
    assert "exposure" in await engine.event(signal(tx="exposure"))
    engine.cfg.paper.exposure_usd = D(250)
    engine.cfg.paper.positions = 1
    assert "positions" in await engine.event(signal(tx="positions"))
    engine.cfg.paper.positions = 10
    with engine.store.session() as s:
        cash = s.get(Cash, "paper:base")
        cash.balance = str(D(cash.balance) - 60)
    assert "daily" in await engine.event(signal(tx="daily"))


async def test_quote_failure_is_not_fake_fill(engine):
    async def fail(*args):
        raise TimeoutError()

    engine.quotes.quote = fail
    assert await engine.event(signal()) == "TimeoutError"
    assert not engine.snapshot()["positions"]


async def test_paper_live_ledgers_separate(engine):
    await engine.event(signal())
    with engine.store.session() as s:
        s.add(Cash(id="live:base", balance="100"))
    assert engine.snapshot()["cash"] == {"base": "974.95"}


async def test_unsigned_restart_cancelled_uncertain_not_retried(engine):
    with engine.store.session() as s:
        for state in ("prepared", "uncertain"):
            s.add(
                Order(
                    id=state,
                    mode="paper",
                    event_key=state,
                    position_id="p",
                    side="buy",
                    state=state,
                    created=now(),
                    data={},
                    signed="",
                    tx_hash="",
                    error="",
                )
            )
    await engine.recover()
    with engine.store.session() as s:
        assert s.get(Order, "prepared").state == "cancelled"
        assert s.get(Order, "uncertain").state == "uncertain"


async def test_paper_fill_uses_minimum_output_plus_adverse_and_fees(engine):
    await engine.event(signal())
    p = engine.snapshot()["positions"][0]
    assert D(p["quantity"]) == D("12.313125")
    assert D(p["cost"]) == D("25.05")
    await engine.close_position(p["id"])
    assert engine.snapshot()["positions"] == []
    with engine.store.session() as s:
        position = s.get(Position, p["id"])
        assert D(position.realized) < 0
        equity = D(s.get(Cash, "paper:base").balance)
        assert abs(equity - (1000 + D(position.realized))) < D(".0000001")


async def test_optional_paper_bridge_uses_quote_and_accounts_for_fees(engine):
    from fomo_spy.config import default_chains

    engine.cfg.chains.append(next(c for c in default_chains() if c.name == "ethereum"))
    engine.cfg.auto_bridge = True
    with engine.store.session() as s:
        s.get(Cash, "paper:base").balance = "0"
        s.add(Cash(id="paper:ethereum", balance="1000"))

    async def bridge(source, destination, usd):
        assert source == "ethereum" and destination == "base" and usd == 100
        return {"spend": "100", "receive": "98.5", "fee": ".5", "provider": "test", "at": now()}

    engine.quotes.bridge = bridge
    assert await engine.event(signal()) == "processed"
    with engine.store.session() as s:
        assert D(s.get(Cash, "paper:ethereum").balance) == D("899.5")
        assert D(s.get(Cash, "paper:base").balance) == D("73.45")
        bridge_entries = [entry for entry in s.scalars(select(Ledger)) if "bridge" in entry.id]
        assert sum(D(entry.cash_delta) for entry in bridge_entries) == -2


async def test_confirmed_paper_settlement_is_idempotent(engine):
    sig = signal()
    await engine.event(sig)
    original = engine.snapshot()["cash"]
    engine.settle("paper:" + sig.key, 999999999, D(0))
    assert engine.snapshot()["cash"] == original


async def test_reconciliation_never_guesses_missing_source(engine):
    import pytest

    from fomo_spy.providers import Unavailable

    await engine.event(signal())
    pid = engine.snapshot()["positions"][0]["id"]
    await engine.reorg("base", 10)
    with pytest.raises(Unavailable, match="verified source wallet"):
        await engine.reconcile_inventory(pid)
    assert engine.snapshot()["positions"][0]["needs_reconcile"]


async def test_trader_excluded_during_quote_cannot_open_position(engine):
    original = engine.quotes.quote

    async def quote_then_exclude(*args):
        quote = await original(*args)
        engine.store.put("excluded", ["demo-7"])
        return quote

    engine.quotes.quote = quote_then_exclude
    assert "deselected" in await engine.event(signal())
    assert not engine.snapshot()["positions"]
