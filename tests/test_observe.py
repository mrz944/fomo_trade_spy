import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "observation", Path(__file__).parents[1] / "deploy/observe.py"
)
observation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(observation)


def test_observation_counts_window_fills_and_checkpoint_progress():
    before = {
        "ok": True,
        "workflow": {"observed_fills": 15},
        "monitoring": [{"key": "wallet", "checkpoint": 10}],
    }
    after = {
        "ok": True,
        "workflow": {"observed_fills": 15, "affected_positions": ["p"]},
        "monitoring": [{"key": "wallet", "checkpoint": 12}],
        "accounting": {"window_fills": {"sell": 2}, "recovered_exits": 2},
    }
    result = observation.observation_metrics([before, after])
    assert not result["fresh_paper_execution_observed"]
    assert result["recovered_exit_fills"] == 2
    assert result["checkpoint_advancement"] == {"wallet": 2}
    assert result["monitoring_gap_samples"] == 1


async def test_accounting_excludes_old_manual_and_delayed_fills_from_fresh(engine):
    from conftest import signal
    from sqlalchemy import select

    from fomo_spy.db import Ledger, Order
    from fomo_spy.domain import now

    started = now()
    for tx in ("old", "manual", "delayed", "fresh"):
        sig = (
            signal(side="sell", tx=tx, quantity=25, source_before=100, source_after=75)
            if tx == "delayed"
            else signal(tx=tx)
        )
        assert await engine.event(sig) == "processed"
    with engine.store.session() as session:
        for order in session.scalars(select(Order)):
            ledger = session.get(Ledger, order.id)
            if order.event_key == signal(tx="old").key:
                ledger.timestamp = started - 1
            elif order.event_key == signal(tx="manual").key:
                order.data = {**order.data, "manual": True}
            elif order.event_key == signal(tx="delayed").key:
                ledger.details = {**ledger.details, "recovery": {"delayed": True}}
    result = observation.accounting(engine.cfg.state_dir / "spy.sqlite", started)
    assert result["window_fills"] == {"buy": 2, "sell": 1}
    assert result["fresh_source_fills"] == {"buy": 1}
    assert result["recovered_exits"] == 1
