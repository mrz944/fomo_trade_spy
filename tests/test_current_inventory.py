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
