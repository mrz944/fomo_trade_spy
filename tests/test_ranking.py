from fomo_spy.demo import history
from fomo_spy.domain import D, Fill, now
from fomo_spy.ranking import rank


def test_evidence_qualifies_immediately_with_no_observation_period():
    result = rank(history(), complete_30d=True)
    assert result["eligible"]
    assert result["completed"] == 25
    assert result["tokens"] == 5
    assert result["score"] > 0
    assert result["without_best_usd"] > "0"


def test_partial_sells_only_count_one_completed_position():
    fills = history()
    sell = fills.pop(1)
    fills.extend(
        [
            sell.model_copy(
                update={
                    "id": "half1",
                    "quantity": D(25),
                    "usd": sell.usd / 2,
                    "fee_usd": sell.fee_usd / 2,
                }
            ),
            sell.model_copy(
                update={
                    "id": "half2",
                    "timestamp": sell.timestamp + 1,
                    "quantity": D(25),
                    "usd": sell.usd / 2,
                    "fee_usd": sell.fee_usd / 2,
                }
            ),
        ]
    )
    result = rank(fills, complete_30d=True)
    assert result["completed"] == 25
    assert result["net_usd"] == rank(history(), complete_30d=True)["net_usd"]


def test_transfers_unknown_costs_and_airdrops_do_not_qualify():
    fills = history()
    fills[0] = fills[0].model_copy(update={"side": "airdrop"})
    fills[2] = fills[2].model_copy(update={"fee_usd": None})
    result = rank(fills, complete_30d=True)
    assert not result["eligible"]
    assert result["tokens"] == 3
    assert len(result["excluded"]) == 2


def test_unknown_inventory_and_missing_coverage_block():
    fills = history()[1:]
    result = rank(fills, complete_30d=False)
    assert not result["eligible"]
    assert any("coverage" in reason for reason in result["reasons"])
    assert any("inventory" in reason for reason in result["excluded"])


def test_concentration_and_fees_can_remove_profitability():
    fills = history()
    for f in fills:
        if f.side == "sell":
            f.usd = D(1000) if f.token.endswith("0") else D(95)
    result = rank(fills, complete_30d=True)
    assert D(result["net_usd"]) > 0
    assert D(result["without_best_usd"]) < 0
    assert not result["eligible"]
    for f in fills:
        f.fee_usd = D(1000)
    assert D(rank(fills, complete_30d=True)["net_usd"]) < 0


def test_duplicates_and_old_positions():
    fills = history()
    result = rank(fills + fills, complete_30d=True)
    assert result["completed"] == 25
    assert rank(fills, complete_30d=True, at=now() + 40 * 86400)["completed"] == 0


def test_cycle_opened_before_window_can_close_inside_window():
    ts = now()
    fills = [
        Fill(
            id="b",
            trader="a",
            chain="base",
            token="t",
            side="buy",
            quantity=1,
            usd=10,
            fee_usd=1,
            timestamp=ts - 40 * 86400,
            provenance="test",
        ),
        Fill(
            id="s",
            trader="a",
            chain="base",
            token="t",
            side="sell",
            quantity=1,
            usd=15,
            fee_usd=1,
            timestamp=ts - 86400,
            provenance="test",
        ),
    ]
    r = rank(fills, complete_30d=True, at=ts)
    assert r["completed"] == 1 and D(r["net_usd"]) == 3
