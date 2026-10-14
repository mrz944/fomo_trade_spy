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
