import copy
from unittest.mock import AsyncMock

import pytest

from fomo_spy.config import Settings, default_chains
from fomo_spy.db import Store
from fomo_spy.demo import DemoQuotes
from fomo_spy.domain import D, now
from fomo_spy.engine import Engine
from fomo_spy.inventory import TOKEN, TOKEN_2022, SolanaCurrent
from fomo_spy.rpc import Monitor

WALLET = "2heJbC32Tpfcb3nbUb5ER61K11FGZVfVGtVnDm6LDogF"


class SolanaFixture:
    def __init__(self, program=TOKEN):
        self.program = program
        self.slot = 10
        self.balances = {"account-a": 0, "account-b": 100_000_000}
        self.transactions = {}

    def tx(self, sig, changes):
        self.slot += 1
        pre = self.balances.copy()
        self.balances.update(changes)
        addresses = [WALLET, *changes]
        delta = sum(self.balances[a] - pre[a] for a in changes)
        quote = -100_000_000 if delta > 0 else 100_000_000

        def rows(values):
            return [
                {
                    "owner": WALLET,
                    "mint": "mint",
                    "accountIndex": addresses.index(a),
                    "programId": self.program,
                    "uiTokenAmount": {"amount": str(values[a]), "decimals": 6},
                }
                for a in changes
            ]

        self.transactions[sig] = {
            "slot": self.slot,
            "version": 1,
            "blockTime": int(now()) + 1,
            "transaction": {"message": {"accountKeys": addresses}},
            "meta": {
                "err": None,
                "fee": 5000,
                "preBalances": [10**9] + [2000000] * len(changes),
                "postBalances": [10**9 + quote - 5000] + [2000000] * len(changes),
                "preTokenBalances": rows(pre),
                "postTokenBalances": rows(self.balances),
            },
        }

    async def call(self, method, params):
        if method == "getBlockTime":
            return int(now())
        if method == "getFirstAvailableBlock":
            return 0
        if method == "getTokenAccountsByOwner":
            program = params[1]["programId"]
            assert program in (TOKEN, TOKEN_2022)
            return {
                "context": {"slot": self.slot},
                "value": [
                    {
                        "pubkey": a,
                        "account": {
                            "owner": self.program,
                            "data": {
                                "parsed": {
                                    "info": {
                                        "owner": WALLET,
                                        "mint": "mint",
                                        "tokenAmount": {"amount": str(n), "decimals": 6},
                                    }
                                }
                            },
                        },
                    }
                    for a, n in self.balances.items()
                    if program == self.program
                ],
            }
        if method == "getSignaturesForAddress":
            return [
                {"signature": sig, "slot": tx["slot"], "err": None}
                for sig, tx in reversed(self.transactions.items())
                if params[0] in tx["transaction"]["message"]["accountKeys"]
            ] + [{"signature": "baseline", "slot": 10, "err": None}]
        if method == "getTransaction":
            return copy.deepcopy(self.transactions[params[0]])
        if method == "getBlock":
            return {
                "blockhash": f"hash{params[0]}",
                "signatures": [
                    sig for sig, tx in self.transactions.items() if tx["slot"] == params[0]
                ],
            }
        raise AssertionError(method)


@pytest.fixture(params=[TOKEN, TOKEN_2022])
def solana(tmp_path, request):
    cfg = Settings(
        selection_policy="paper_research",
        state_dir=tmp_path / "private",
        chains=[default_chains()[0]],
    )
    store = Store(cfg.state_dir / "spy.sqlite")
    store.put("current_leaderboard", {"traders": ["u"], "expires": now() + 1000})
    store.put("trader:u", {"userId": "u", "rank": 1, "pnlUsd": 100, "wallets": {"solana": WALLET}})
    store.put("current_capability:solana", {"at": now(), "observe_ready": True})
    engine = Engine(cfg, store, DemoQuotes(cfg))
    engine.research.refresh(at=now() - 1)
    scenario = SolanaFixture(request.param)
    rpc = AsyncMock()
    rpc.chain = cfg.chain("solana")
    rpc.call.side_effect = scenario.call
    monitor = Monitor(cfg, store, None, lambda _: {}, engine.event, engine.reorg, engine.health)
    yield engine, monitor, rpc, scenario
    store.close()


async def test_whole_mint_inventory_partial_exit_restart_and_full_exit(solana):
    engine, monitor, rpc, scenario = solana
    tracker = SolanaCurrent(monitor)
    await tracker.scan(rpc, "u", WALLET)  # Only establish the baseline.
    assert not engine.snapshot()["orders"]
    scenario.tx("buy", {"account-a": 100_000_000})
    await tracker.scan(rpc, "u", WALLET)
    assert len(engine.snapshot()["positions"]) == 1, engine.snapshot()["activity"]
    bought = D(engine.snapshot()["positions"][0]["quantity"])
    scenario.tx("partial", {"account-a": 50_000_000})
    await tracker.scan(rpc, "u", WALLET)
    remaining = D(engine.snapshot()["positions"][0]["quantity"])
    assert abs(remaining - bought * D(".75")) < D(".000001")
    restarted = Engine(engine.cfg, engine.store, engine.quotes)
    monitor.emit = restarted.event
    tracker = SolanaCurrent(monitor)
    await tracker.scan(rpc, "u", WALLET)
    assert len(restarted.snapshot()["orders"]) == 2
    scenario.tx("full", {"account-a": 0, "account-b": 0})
    await tracker.scan(rpc, "u", WALLET)
    assert restarted.snapshot()["positions"] == []
    assert len(restarted.snapshot()["orders"]) == 3


async def test_unobserved_account_change_flags_open_lot_without_guessing(solana):
    engine, monitor, rpc, scenario = solana
    tracker = SolanaCurrent(monitor)
    await tracker.scan(rpc, "u", WALLET)
    scenario.tx("buy", {"account-a": 100_000_000})
    await tracker.scan(rpc, "u", WALLET)
    scenario.slot += 1
    scenario.balances["account-b"] += 25_000_000
    with pytest.raises(Exception, match="do not reconcile"):
        await tracker.scan(rpc, "u", WALLET)
    assert engine.snapshot()["positions"][0]["needs_reconcile"]
    assert len(engine.snapshot()["orders"]) == 1


async def test_missing_transaction_does_not_advance_inventory_checkpoint(solana):
    engine, monitor, rpc, scenario = solana
    tracker = SolanaCurrent(monitor)
    await tracker.scan(rpc, "u", WALLET)
    scenario.tx("buy", {"account-a": 100_000_000})
    original = scenario.call

    async def missing(method, params):
        return None if method == "getTransaction" else await original(method, params)

    rpc.call.side_effect = missing
    with pytest.raises(Exception, match="transaction unavailable"):
        await tracker.scan(rpc, "u", WALLET)
    rpc.call.side_effect = original
    await tracker.scan(rpc, "u", WALLET)
    assert len(engine.snapshot()["orders"]) == 1


async def test_reselection_baselines_without_replaying_intervening_purchase(solana):
    from fomo_spy.ipc import Control

    engine, monitor, rpc, scenario = solana
    tracker = SolanaCurrent(monitor)
    await tracker.scan(rpc, "u", WALLET)
    control = Control(engine)
    await control.dispatch({"command": "exclude", "trader": "u"})
    scenario.tx("while-excluded", {"account-a": 100_000_000})
    await control.dispatch({"command": "include", "trader": "u"})
    await tracker.scan(rpc, "u", WALLET)
    assert not engine.snapshot()["orders"]
    scenario.tx("after-reselection", {"account-a": 200_000_000})
    await tracker.scan(rpc, "u", WALLET)
    assert len(engine.snapshot()["orders"]) == 1


@pytest.mark.parametrize(
    "method",
    [
        "getTokenAccountsByOwner",
        "getBlockTime",
        "getSignaturesForAddress",
        "getTransaction",
        "getBlock",
    ],
)
async def test_throttle_resume_keeps_target_and_checkpoint(solana, method):
    from fomo_spy.db import MonitorState
    from fomo_spy.rpc import RPCBackoff

    engine, monitor, rpc, scenario = solana
    await SolanaCurrent(monitor).scan(rpc, "u", WALLET)
    scenario.tx("buy", {"account-a": 100_000_000})
    original = scenario.call
    failed = False

    async def interrupted(name, params):
        nonlocal failed
        if name == method and not failed:
            failed = True
            raise RPCBackoff(60)
        return await original(name, params)

    rpc.call.side_effect = interrupted
    with pytest.raises(RPCBackoff):
        await SolanaCurrent(monitor).scan(rpc, "u", WALLET)
    key = f"solana:u:{WALLET}:{scenario.program}"
    with engine.store.session() as session:
        assert session.get(MonitorState, key).data["slot"] == 10
    rpc.call.side_effect = original
    await SolanaCurrent(monitor).scan(rpc, "u", WALLET)
    assert len(engine.snapshot()["orders"]) == 1


async def test_missed_exits_restart_current_quotes_transfers_and_no_old_buys(solana):
    from sqlalchemy import select

    from fomo_spy.db import Ledger

    engine, monitor, rpc, scenario = solana
    await SolanaCurrent(monitor).scan(rpc, "u", WALLET)
    scenario.tx("buy", {"account-a": 100_000_000})
    await SolanaCurrent(monitor).scan(rpc, "u", WALLET)
    bought = D(engine.snapshot()["positions"][0]["quantity"])
    scenario.tx("intervening-buy", {"account-b": 200_000_000})
    scenario.tx("transfer", {"account-a": 150_000_000})
    tx = scenario.transactions["transfer"]
    tx["transaction"]["message"]["instructions"] = [
        {
            "programId": scenario.program,
            "parsed": {"type": "transferChecked", "info": {"destination": "account-a"}},
        }
    ]
    tx["meta"]["postBalances"][0] = tx["meta"]["preBalances"][0] - 5000
    scenario.tx("partial", {"account-a": 80_000_000})  # 70 / 350 = 20%
    engine = Engine(engine.cfg, engine.store, engine.quotes)
    engine.started = now() + 2
    monitor.started = engine.started
    monitor.emit = engine.event
    engine.store.put("paused:paper", True)
    engine.store.put("excluded", ["u"])
    engine.quotes.prices["mint"] = D(4)
    await SolanaCurrent(monitor).scan(rpc, "u", WALLET)
    remaining = D(engine.snapshot()["positions"][0]["quantity"])
    assert abs(remaining - bought * D(".8")) < D(".000001")
    assert not engine.snapshot()["positions"][0]["needs_reconcile"]
    assert len(engine.snapshot()["orders"]) == 2
    scenario.tx("full", {"account-a": 0, "account-b": 0})
    await SolanaCurrent(monitor).scan(rpc, "u", WALLET)
    await SolanaCurrent(monitor).scan(rpc, "u", WALLET)
    assert not engine.snapshot()["positions"]
    with engine.store.session() as session:
        ledger = list(session.scalars(select(Ledger)))
        assert len(ledger) == 3
        exits = [r for r in ledger if r.details["side"] == "sell"]
        assert all(r.details["recovery"]["delayed"] for r in exits)
        assert all(D(r.details["execution_price_usd"]) > D("3.9") for r in exits)
        assert sum(D(r.details["fee_usd"]) for r in ledger) == D(".15")
        assert D(engine.snapshot()["cash"]["solana"]) == engine.cfg.limits.capital_usd + sum(
            D(r.cash_delta) for r in ledger
        )


@pytest.mark.parametrize(
    "boundary", ["before_delivery", "before_settle", "after_settle", "after_delivery"]
)
async def test_crash_delivery_settlement_is_idempotent(solana, boundary):
    from sqlalchemy import select

    from fomo_spy.db import Ledger

    engine, monitor, rpc, scenario = solana
    await SolanaCurrent(monitor).scan(rpc, "u", WALLET)
    scenario.tx("buy", {"account-a": 100_000_000})
    await SolanaCurrent(monitor).scan(rpc, "u", WALLET)
    scenario.tx("sale", {"account-a": 0, "account-b": 0})
    emit, settle = monitor.emit, engine.settle

    class Crash(BaseException):
        pass

    async def interrupted(sig, **kwargs):
        if boundary == "before_delivery":
            raise Crash()
        result = await emit(sig, **kwargs)
        if boundary == "after_delivery":
            raise Crash()
        return result

    def settlement(*args):
        if boundary == "before_settle":
            raise Crash()
        settle(*args)
        if boundary == "after_settle":
            raise Crash()

    monitor.emit, engine.settle = interrupted, settlement
    with pytest.raises(Crash):
        await SolanaCurrent(monitor).scan(rpc, "u", WALLET)
    restarted = Engine(engine.cfg, engine.store, engine.quotes)
    await restarted.recover()
    monitor.emit = restarted.event
    await SolanaCurrent(monitor).scan(rpc, "u", WALLET)
    await SolanaCurrent(monitor).scan(rpc, "u", WALLET)
    assert not restarted.snapshot()["positions"]
    with engine.store.session() as session:
        assert len(list(session.scalars(select(Ledger)))) == 2


async def test_quote_retry_and_both_program_readiness(solana):
    from fomo_spy.providers import Unavailable

    engine, monitor, rpc, scenario = solana
    await SolanaCurrent(monitor).scan(rpc, "u", WALLET)
    scenario.tx("buy", {"account-a": 100_000_000})
    await SolanaCurrent(monitor).scan(rpc, "u", WALLET)
    scenario.tx("sale", {"account-a": 0, "account-b": 0})
    quote = engine.quotes.quote
    engine.quotes.quote = AsyncMock(side_effect=Unavailable("temporary quote failure"))
    await SolanaCurrent(monitor).scan(rpc, "u", WALLET)
    assert engine.snapshot()["workflow"]["state"] == "exit monitoring degraded"
    assert engine.snapshot()["workflow"]["pending_recoveries"] == 1
    assert not engine.monitor_is_ready("solana", "u")
    engine.quotes.quote = quote
    await SolanaCurrent(monitor).scan(rpc, "u", WALLET)
    assert not engine.snapshot()["positions"]


async def test_pagination_past_1000_is_durable(solana):
    from fomo_spy.db import MonitorState
    from fomo_spy.inventory import ScanYield

    engine, monitor, rpc, scenario = solana
    tracker = SolanaCurrent(monitor)
    tracker.key = "pagination-test"
    tracker.state = {"scan": {"pages": {}}}
    tracker.requests = tracker.pages = 0
    rows = [{"signature": str(n), "slot": n, "err": None} for n in range(1200, -1, -1)]

    async def page(method, params):
        assert method == "getSignaturesForAddress"
        before = params[1].get("before")
        start = 1201 - int(before) if before is not None else 0
        return rows[start : start + 100]

    rpc.call.side_effect = page
    with pytest.raises(ScanYield):
        await tracker.signatures(rpc, WALLET, 0, 1200)
    with engine.store.session() as session:
        state = copy.deepcopy(session.get(MonitorState, tracker.key).data)
    tracker = SolanaCurrent(monitor)
    tracker.key, tracker.state = "pagination-test", state
    tracker.requests = tracker.pages = 0
    result = await tracker.signatures(rpc, WALLET, 0, 1200)
    assert len(result) == 1200
    assert tracker.pages == 3


async def test_repeated_page_and_canonical_conflict_do_not_commit(solana):
    from fomo_spy.db import MonitorState
    from fomo_spy.providers import Unavailable

    engine, monitor, rpc, scenario = solana
    await SolanaCurrent(monitor).scan(rpc, "u", WALLET)
    scenario.tx("buy", {"account-a": 100_000_000})
    original = scenario.call

    async def repeated(method, params):
        if method == "getSignaturesForAddress":
            return [{"signature": "buy", "slot": 11}]
        return await original(method, params)

    rpc.call.side_effect = repeated
    with pytest.raises(Unavailable, match="repeated"):
        await SolanaCurrent(monitor).scan(rpc, "u", WALLET)

    async def conflict(method, params):
        if method == "getSignaturesForAddress" and params[1].get("before"):
            return [{"signature": "baseline", "slot": 10}]
        if method == "getBlock":
            return {"blockhash": "wrong", "signatures": ["other"]}
        return await original(method, params)

    rpc.call.side_effect = conflict
    with pytest.raises(Unavailable, match="absent"):
        await SolanaCurrent(monitor).scan(rpc, "u", WALLET)
    with engine.store.session() as session:
        state = session.get(MonitorState, f"solana:u:{WALLET}:{scenario.program}").data
        assert state["slot"] == 10
    assert not engine.snapshot()["orders"]


async def test_recorded_unexecuted_sale_revalidated_from_committed_inventory(solana):
    from fomo_spy.db import Event, MonitorState
    from fomo_spy.normalization import solana_movements

    engine, monitor, rpc, scenario = solana
    await SolanaCurrent(monitor).scan(rpc, "u", WALLET)
    scenario.tx("buy", {"account-a": 100_000_000})
    await SolanaCurrent(monitor).scan(rpc, "u", WALLET)
    bought = D(engine.snapshot()["positions"][0]["quantity"])
    scenario.tx("missed", {"account-a": 50_000_000})
    sig = solana_movements(
        scenario.transactions["missed"], "u", WALLET, "missed", rpc.chain, now() + 3
    )[0].signal
    sig.block_hash = f"hash{scenario.slot}"
    await engine.event(sig)
    key = f"solana:u:{WALLET}:{scenario.program}"
    # Reproduce the old monitor's commit after a historically rejected sale.
    with engine.store.session() as session:
        row = session.get(MonitorState, key)
        state = copy.deepcopy(row.data)
        state["slot"] = scenario.slot
        state["accounts"]["account-a"]["amount"] = 50_000_000
        row.data = state
    await SolanaCurrent(monitor).scan(rpc, "u", WALLET)
    assert abs(D(engine.snapshot()["positions"][0]["quantity"]) - bought * D(".75")) < D(".000001")
    with engine.store.session() as session:
        event = session.get(Event, sig.key)
        assert event.status == "processed"
        assert "historical/replay event: never copies" in event.data["rejection_history"]
        assert session.get(MonitorState, event.data["recovery"]["evidence_key"])
    await SolanaCurrent(monitor).scan(rpc, "u", WALLET)
    assert len(engine.snapshot()["orders"]) == 2


async def test_large_account_set_yields_and_finishes(solana):
    from fomo_spy.db import MonitorState

    engine, monitor, rpc, scenario = solana
    for n in range(45):
        scenario.balances[f"account-{n}"] = 1_000_000
    await SolanaCurrent(monitor).scan(rpc, "u", WALLET)
    scenario.tx("buy", {"account-a": 100_000_000})
    key = f"solana:u:{WALLET}:{scenario.program}"
    for _ in range(12):
        await SolanaCurrent(monitor).scan(rpc, "u", WALLET)
        with engine.store.session() as session:
            state = session.get(MonitorState, key).data
        if state["slot"] == scenario.slot:
            break
    assert state["slot"] == scenario.slot
    assert len(engine.snapshot()["orders"]) == 1


async def test_fresh_transport_marks_and_old_fills_cannot_hide_stale_exits(solana):
    engine, monitor, rpc, scenario = solana
    await SolanaCurrent(monitor).scan(rpc, "u", WALLET)
    scenario.tx("buy", {"account-a": 100_000_000})
    await SolanaCurrent(monitor).scan(rpc, "u", WALLET)
    engine.store.put("environment", "real")
    engine.store.put("excluded", ["u"])
    engine.health("rpc:solana", {"state": "connected", "at": now()})
    engine.store.put(
        "monitor_ready:solana:u", {"ready": True, "at": now(), "covered_at": now() - 121}
    )
    snap = engine.snapshot()
    assert snap["workflow"]["state"] == "exit monitoring degraded"
    assert snap["workflow"]["entry_ready"] == 0
    assert snap["workflow"]["affected_positions"]
    with engine.store.session() as session:
        assert "exit monitoring" in engine.risk_reason(session, "solana", D(25))


async def test_one_program_failure_cannot_be_overwritten(solana):
    from fomo_spy.providers import Unavailable

    engine, monitor, rpc, scenario = solana
    await SolanaCurrent(monitor).scan(rpc, "u", WALLET)
    original = scenario.call

    async def fail(method, params):
        if method == "getTokenAccountsByOwner" and params[1]["programId"] == TOKEN:
            raise Unavailable("token program unavailable")
        return await original(method, params)

    rpc.call.side_effect = fail
    with pytest.raises(Unavailable):
        await SolanaCurrent(monitor).scan(rpc, "u", WALLET)
    assert not engine.monitor_is_ready("solana", "u")


async def test_unexplained_transfer_preserves_gap_and_blocks_balance_reconciliation(solana):
    from fomo_spy.providers import Unavailable

    engine, monitor, rpc, scenario = solana
    await SolanaCurrent(monitor).scan(rpc, "u", WALLET)
    scenario.tx("buy", {"account-a": 100_000_000})
    await SolanaCurrent(monitor).scan(rpc, "u", WALLET)
    scenario.tx("unknown-route", {"account-a": 50_000_000})
    tx = scenario.transactions["unknown-route"]
    tx["meta"]["postBalances"][0] = tx["meta"]["preBalances"][0] - 5000
    with pytest.raises(Unavailable, match="do not reconcile"):
        await SolanaCurrent(monitor).scan(rpc, "u", WALLET)
    position = engine.snapshot()["positions"][0]
    assert position["needs_reconcile"]
    with pytest.raises(Unavailable, match="current balance cannot"):
        await engine.reconcile_inventory(position["id"])


async def test_target_snapshot_does_not_move_after_interruption(solana):
    from fomo_spy.db import MonitorState
    from fomo_spy.rpc import RPCBackoff

    engine, monitor, rpc, scenario = solana
    await SolanaCurrent(monitor).scan(rpc, "u", WALLET)
    scenario.tx("buy", {"account-a": 100_000_000})
    original = scenario.call

    async def fail(method, params):
        if method == "getTransaction":
            raise RPCBackoff(60)
        return await original(method, params)

    rpc.call.side_effect = fail
    with pytest.raises(RPCBackoff):
        await SolanaCurrent(monitor).scan(rpc, "u", WALLET)
    scenario.tx("later", {"account-a": 150_000_000})
    rpc.call.side_effect = original
    await SolanaCurrent(monitor).scan(rpc, "u", WALLET)
    with engine.store.session() as session:
        state = session.get(MonitorState, f"solana:u:{WALLET}:{TOKEN}").data
        assert state["slot"] == 11
        if scenario.program == TOKEN:
            assert state["accounts"]["account-a"]["amount"] == 100_000_000
    await SolanaCurrent(monitor).scan(rpc, "u", WALLET)
    assert len(engine.snapshot()["orders"]) == 2


async def test_recovery_uses_production_relay_exact_size_current_quote(solana):
    import json

    import httpx

    from fomo_spy.providers import Relay

    engine, monitor, rpc, scenario = solana
    price, requests = D(2), []
    chain = engine.cfg.chain("solana")

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        amount = int(body["amount"])
        buy = body["originCurrency"] == chain.settlement
        output = int(D(amount) / price) if buy else int(D(amount) * price)

        def currency(token, quantity):
            return {
                "currency": {"chainId": chain.relay_id, "address": token, "decimals": 6},
                "amount": str(quantity),
                "minimumAmount": str(quantity * 99 // 100),
                "amountUsd": str(D(quantity) / 10**6),
            }

        return httpx.Response(
            200,
            json={
                "steps": [{"kind": "transaction"}],
                "details": {
                    "sender": body["user"],
                    "recipient": body["recipient"],
                    "currencyIn": currency(body["originCurrency"], amount),
                    "currencyOut": currency(body["destinationCurrency"], output),
                },
                "fees": {"gas": {"amountUsd": ".05"}},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        engine.quotes = Relay(engine.cfg, http)
        await SolanaCurrent(monitor).scan(rpc, "u", WALLET)
        scenario.tx("buy", {"account-a": 100_000_000})
        await SolanaCurrent(monitor).scan(rpc, "u", WALLET)
        bought = D(engine.snapshot()["positions"][0]["quantity"])
        scenario.tx("partial", {"account-a": 50_000_000})
        engine.started = monitor.started = now() + 2
        price = D(4)
        await SolanaCurrent(monitor).scan(rpc, "u", WALLET)
        assert int(requests[-1]["amount"]) == int(bought * D(".25") * 10**6)
        assert requests[-1]["tradeType"] == "EXACT_INPUT"
        scenario.tx("full", {"account-a": 0, "account-b": 0})
        await SolanaCurrent(monitor).scan(rpc, "u", WALLET)
        assert not engine.snapshot()["positions"]


async def test_isolated_backup_recovery_verifier(solana, tmp_path):
    import importlib.util
    import sqlite3
    from pathlib import Path

    engine, monitor, rpc, scenario = solana
    await SolanaCurrent(monitor).scan(rpc, "u", WALLET)
    scenario.tx("buy", {"account-a": 100_000_000})
    await SolanaCurrent(monitor).scan(rpc, "u", WALLET)
    backup = tmp_path / "backup.sqlite"
    with (
        sqlite3.connect(engine.cfg.state_dir / "spy.sqlite") as source,
        sqlite3.connect(backup) as target,
    ):
        source.backup(target)
    spec = importlib.util.spec_from_file_location(
        "verify_recovery", Path(__file__).parents[1] / "deploy/verify_recovery.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # The production backup contains all seven prefunded chains.
    original = module.Settings
    module.Settings = lambda **kwargs: original(**kwargs, chains=engine.cfg.chains)
    await module.verify(backup, tmp_path / "isolated.sqlite")
    assert engine.snapshot()["positions"]
    with pytest.raises(ValueError, match="isolated"):
        await module.verify(backup, backup)
