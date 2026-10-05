import pytest
from conftest import signal
from pydantic import ValidationError
from sqlalchemy import select

from fomo_spy.config import Settings
from fomo_spy.db import Ledger, Order, Store
from fomo_spy.demo import DemoQuotes
from fomo_spy.domain import D, now
from fomo_spy.engine import Engine
from fomo_spy.providers import Unavailable
from fomo_spy.selection import ResearchPolicy

WALLET = "0x" + "1" * 40


def candidate(store, uid, rank=1, pnl=10):
    store.put(
        "trader:" + uid, {"userId": uid, "rank": rank, "pnlUsd": pnl, "wallets": {"evm": WALLET}}
    )


@pytest.fixture
def research(cfg):
    cfg.selection_policy = "paper_research"
    cfg.follow = 1
    store = Store(cfg.state_dir / "spy.sqlite")
    store.put("environment", "real")
    candidate(store, "demo-7")
    store.put("current_leaderboard", {"traders": ["demo-7"], "expires": now() + 21600})
    store.put("current_capability:base", {"at": now(), "observe_ready": True})
    store.put(
        "ranking:demo-7",
        {
            "eligible": False,
            "reasons": ["30-day evidence incomplete"],
            "missing": ["archive unavailable"],
            "score": None,
            "evaluated_at": now(),
        },
    )
    engine = Engine(cfg, store, DemoQuotes(cfg))
    engine.research.refresh(at=now() - 1)
    store.put("monitor_ready:base:demo-7", {"ready": True, "at": now(), "anchor": 1})
    yield engine
    store.close()


def test_research_cannot_authorize_live_and_history_defaults_off():
    assert not Settings(selection_policy="paper_research").backfill_enabled
    assert Settings().backfill_enabled
    with pytest.raises(ValidationError, match="cannot authorize live"):
        Settings(mode="live", selection_policy="paper_research")


async def test_research_buy_partial_exit_full_exit_restart_accounting(research):
    e = research
    assert e.selected() == ["demo-7"]
    assert not e.store.get("ranking:demo-7")["eligible"]
    assert await e.event(signal()) == "processed"
    before = D(e.snapshot()["positions"][0]["quantity"])
    e.store.put("excluded", ["demo-7"])
    e.store.put("paused:paper", True)
    assert e.selected() == [] and e.watched("base") == ["demo-7"]
    assert (
        await e.event(
            signal(
                side="sell", tx="partial", quantity=D(25), source_before=D(100), source_after=D(75)
            )
        )
        == "processed"
    )
    remaining = D(e.snapshot()["positions"][0]["quantity"])
    assert abs(remaining - before * D(".75")) < D(".000001")
    restarted = Engine(e.cfg, e.store, e.quotes)
    assert (
        await restarted.event(
            signal(side="sell", tx="full", quantity=D(75), source_before=D(75), source_after=D(0))
        )
        == "processed"
    )
    assert restarted.snapshot()["positions"] == []
    with e.store.session() as session:
        rows = list(session.scalars(select(Ledger)))
        assert len(rows) == 3
        assert all(r.details["selection_policy"] == "paper_research" for r in rows)
        assert D(restarted.snapshot()["cash"]["base"]) == 1000 + sum(D(r.cash_delta) for r in rows)
        assert all(
            o.data["selection_policy"] == "paper_research" for o in session.scalars(select(Order))
        )


async def test_research_requires_liquidation_route_and_never_fakes_fill(research):
    original = research.quotes.quote

    async def quotes(chain, token, side, amount, decimals):
        if side == "sell":
            raise Unavailable("no executable liquidation route")
        return await original(chain, token, side, amount, decimals)

    research.quotes.quote = quotes
    assert "liquidation" in await research.event(signal())
    assert not research.snapshot()["positions"]
    assert research.snapshot()["cash"]["base"] == "1000"


async def test_research_requires_activation_and_ready_inventory(research):
    research.store.put("monitor_ready:base:demo-7", {"ready": False, "at": now()})
    assert "baseline" in await research.event(signal())
    assert not research.snapshot()["orders"]


def test_current_board_staleness_exclusion_and_rotation(research):
    e = research
    candidate(e.store, "active", rank=10)
    e.store.put("current_leaderboard", {"traders": ["demo-7", "active"], "expires": now() + 21600})
    e.store.put("research_activity:active:base", {"at": now()})
    policy = ResearchPolicy(e.cfg, e.store)
    policy.refresh()
    assert e.selected() == ["demo-7"]  # Minimum dwell avoids selection churn.
    policy.refresh(at=now() + 1801)
    assert e.selected() == ["active"]
    e.store.put("excluded", ["active"])
    assert e.selected() == []
    policy.refresh()
    assert e.selected() == ["demo-7"]
    e.store.put("current_leaderboard", {"traders": [], "expires": now() - 1})
    assert e.selected() == []


async def test_selection_rechecked_after_quote(research):
    original = research.quotes.quote

    async def quotes(*args):
        q = await original(*args)
        research.store.put("excluded", ["demo-7"])
        return q

    research.quotes.quote = quotes
    assert "deselected" in await research.event(signal())
    assert research.snapshot()["positions"] == []


async def test_policy_cannot_mix_with_existing_verified_lot(research):
    assert await research.event(signal()) == "processed"
    research.cfg.selection_policy = "verified"
    research.store.put("ranking:demo-7", {"eligible": True, "score": 1, "evaluated_at": now()})
    assert "different selection policy" in await research.event(signal(tx="second"))


async def test_selection_epoch_clears_unwatched_inventory_without_losing_open_lots(research):
    from fomo_spy.ipc import Control
    from fomo_spy.rpc import Monitor

    e = research
    monitor = Monitor(e.cfg, e.store, None, lambda _: {}, e.event, e.reorg, e.health)
    key = "source_inventory:base:demo-7:token"
    e.store.put(key, {"quantity": "999"})
    assert monitor.new_activation("base", "demo-7")
    assert e.store.get(key) is None
    e.store.put("monitor_ready:base:demo-7", {"ready": True, "at": now(), "anchor": 1})
    assert await e.event(signal()) == "processed"
    e.store.put(key, {"quantity": "100"})
    control = Control(e)
    await control.dispatch({"command": "exclude", "trader": "demo-7"})
    await control.dispatch({"command": "include", "trader": "demo-7"})
    assert not monitor.new_activation("base", "demo-7")
    assert e.store.get(key)["quantity"] == "100"
