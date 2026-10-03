import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import func, select

from fomo_spy.config import default_chains
from fomo_spy.daemon import Daemon
from fomo_spy.db import Observation, ScanCheckpoint, Store
from fomo_spy.domain import now
from fomo_spy.history import History, read
from fomo_spy.normalization import solana_movements
from fomo_spy.providers import Unavailable
from fomo_spy.rpc import RPC, RPCBackoff


async def test_unselected_alert_is_visible_without_creating_trade(cfg):
    daemon = Daemon(cfg)
    try:
        alert = {
            "userId": "unselected",
            "eventId": "live-1",
            "alertType": "buy",
            "ts": now() * 1000,
            "chainId": 8453,
            "token": {"address": "token"},
        }
        await daemon.social(alert)
        await daemon.social(alert)
        snapshot = daemon.engine.snapshot()
        assert snapshot["selected"] == snapshot["watching"] == []
        assert snapshot["orders"] == snapshot["positions"] == []
        assert snapshot["workflow"]["latest_trade_decision"] is None
        (row,) = snapshot["activity"]
        assert row["kind"] == "FOMO alert"
        assert row["chain"] == "base"
        assert "not selected" in row["reason"]
        with daemon.store.session() as session:
            assert session.scalar(select(func.count()).select_from(Observation)) == 1
    finally:
        await daemon.http.aclose()
        daemon.store.close()
        daemon.lockfile.close()


def solana_tx(at):
    return {
        "version": 1,
        "slot": 100,
        "blockTime": at,
        "transaction": {
            "message": {
                "accountKeys": [{"pubkey": "wallet"}],
                "transactionConfig": {"computeUnitLimit": 30000, "priorityFee": 2000},
            }
        },
        "meta": {
            "err": None,
            "fee": 7000,
            "preBalances": [10**9],
            "postBalances": [10**9 - 10**8 - 7000],
            "preTokenBalances": [],
            "postTokenBalances": [
                {
                    "owner": "wallet",
                    "mint": "token",
                    "uiTokenAmount": {"amount": "1000000", "decimals": 6},
                }
            ],
        },
    }


def test_v1_uses_actual_meta_fee_not_compute_instruction_guesses():
    from fomo_spy.domain import D

    (movement,) = solana_movements(
        solana_tx(int(now())), "u", "wallet", "sig", default_chains()[0], 0
    )
    assert movement.signal.side == "buy"
    assert movement.consideration == D(".1")
    assert movement.gas_native == D(".000007")


def test_captured_solana_airdrop_is_not_a_purchase_or_wallet_paid_fee():
    fixture = json.loads(
        (Path(__file__).parent / "fixtures/solana-transfer-captured.json").read_text()
    )
    (movement,) = solana_movements(
        fixture["transaction"],
        "captured",
        fixture["wallet"],
        fixture["signature"],
        default_chains()[0],
        float("inf"),
    )
    assert movement.signal.side == "transfer"
    assert movement.signal.quantity == 2000000
    assert movement.consideration is None
    assert movement.gas_native == 0  # The sender paid meta.fee, not the candidate.


async def test_solana_page_progress_survives_mid_page_failure(cfg):
    chain = default_chains()[0]
    cfg.chains = [chain]
    store = Store(cfg.state_dir / "spy.sqlite")
    rpc = AsyncMock()
    rpc.chain = chain
    timestamp = int(now())
    fail = True
    seen = []

    async def call(method, params):
        nonlocal fail
        if method == "getSignaturesForAddress":
            return (
                [{"signature": "b", "err": None}]
                if params[1].get("before") == "a"
                else [{"signature": "a", "err": None}, {"signature": "b", "err": None}]
            )
        if method == "getTransaction":
            assert params[1]["maxSupportedTransactionVersion"] == 1
            seen.append(params[0])
            if params[0] == "b" and fail:
                raise RPCBackoff(60)
            return solana_tx(timestamp)
        raise AssertionError(method)

    rpc.call.side_effect = call
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"coins": {}}))
    ) as http:
        history = History(cfg, store, http, lambda *_: None)
        with pytest.raises(RPCBackoff):
            await history.solana_step(rpc, {"userId": "u"}, "wallet", "u:solana:wallet")
        assert read(store, ScanCheckpoint, "u:solana:wallet")["before"] == "a"
        store.close()
        store = Store(cfg.state_dir / "spy.sqlite")
        history = History(cfg, store, http, lambda *_: None)
        fail = False
        await history.solana_step(rpc, {"userId": "u"}, "wallet", "u:solana:wallet")
        assert seen == ["a", "b", "b"]
        checkpoint = read(store, ScanCheckpoint, "u:solana:wallet")
        assert checkpoint["transactions"] == 2
        assert not checkpoint["complete"]
    store.close()


async def test_rpc_rate_limit_applies_across_instances_without_repeated_requests(cfg):
    calls = []

    def handler(req):
        calls.append(json.loads(req.content))
        return httpx.Response(429, headers={"retry-after": "120"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        for _ in range(2):
            with pytest.raises(RPCBackoff):
                await RPC(cfg.chain("base"), http, historical=True).call("eth_blockNumber")
    assert len(calls) == 1


@pytest.mark.parametrize(
    "status,code,message,expected",
    [
        (
            403,
            -32602,
            "Archive requests require a personal token. https://provider.test/secret",
            "archive access requires a provider token",
        ),
        (
            200,
            -32602,
            "historical state not available https://provider.test/secret",
            "historical state unavailable",
        ),
        (200, -32601, "Method not found", "method unsupported by endpoint"),
        (200, 15, "You reached Public endpoint rate limit", "RPC rate limited"),
    ],
)
async def test_rpc_errors_explain_blocker_without_persisting_provider_secrets(
    cfg, status, code, message, expected
):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(status, json={"error": {"code": code, "message": message}})
        )
    ) as http:
        with pytest.raises(Unavailable, match=expected) as error:
            await RPC(cfg.chain("base"), http).call("eth_getBalance")
        assert "secret" not in str(error.value)


async def test_block_search_never_needs_pruned_ancient_midpoints():
    from fomo_spy.history import block_at

    head, target = 10_000_000, 9_990_000
    rpc = AsyncMock()

    async def call(method, params):
        assert method == "eth_getBlockByNumber"
        height = int(params[0], 16)
        assert height >= head - 20000, "request crossed available recent history"
        return {"timestamp": hex(height * 12)}

    rpc.call.side_effect = call
    assert await block_at(rpc, target * 12 + 5, head) == target


async def test_initial_rpc_failure_does_not_permanently_kill_monitor(cfg, monkeypatch):
    from fomo_spy.rpc import Monitor

    cfg.rpc_poll_seconds = 0.2
    chain = cfg.chain("base")
    chain.ws = ""
    check = AsyncMock(side_effect=[RPCBackoff(60), chain.fomo_id])
    monkeypatch.setattr(RPC, "check_network", check)
    states = []

    def health(name, data):
        states.append(data["state"])
        if data["state"] == "connected":
            monitor.stop.set()

    monitor = Monitor(cfg, None, None, lambda _: {}, AsyncMock(), AsyncMock(), health)
    await monitor.run_chain(chain)
    assert states == ["degraded", "connected"]
    assert check.await_count == 2
