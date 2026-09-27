from unittest.mock import AsyncMock

from fomo_spy.config import default_chains
from fomo_spy.domain import D, now
from fomo_spy.rpc import TRANSFER, Monitor, log_delta, solana_signals, topic


def test_log_deltas_ignore_nfts_and_unrelated_wallets():
    wallet = "0x" + "1" * 40
    log = {"topics": [TRANSFER, topic(wallet), topic("0x" + "2" * 40)], "data": hex(123)}
    assert log_delta(log, wallet) == -123
    assert log_delta(log, "0x" + "2" * 40) == 123
    assert log_delta(log, "0x" + "3" * 40) == 0
    log["topics"].append("nft-id")
    assert log_delta(log, wallet) == 0


def test_solana_atomic_balances_and_transfer_classification():
    chain = default_chains()[0]

    def balance(mint, amount):
        return {
            "owner": "wallet",
            "mint": mint,
            "uiTokenAmount": {"amount": str(amount), "decimals": 6},
        }

    tx = {
        "slot": 123,
        "blockTime": int(now()),
        "meta": {
            "err": None,
            "preTokenBalances": [balance(chain.settlement, 100000000), balance("TOKEN", 0)],
            "postTokenBalances": [balance(chain.settlement, 75000000), balance("TOKEN", 10000000)],
        },
    }
    signals = solana_signals(tx, "trader", "wallet", "signature", chain, 0)
    assert signals[0].side == "buy"
    assert signals[0].quantity == D(10)
    tx["meta"]["postTokenBalances"][0] = balance(chain.settlement, 100000000)
    assert solana_signals(tx, "trader", "wallet", "signature", chain, 0)[0].side == "transfer"


async def test_evm_restart_overlap_and_reorg(engine):
    chain = engine.cfg.chain("base")
    rpc = AsyncMock()
    rpc.chain = chain

    async def call(method, params=None):
        if method == "eth_blockNumber":
            return hex(102)
        if method == "eth_getBlockByNumber":
            return {"hash": "canonical", "timestamp": hex(int(now()))}
        if method == "eth_getLogs":
            return []
        raise AssertionError(method)

    rpc.call.side_effect = call
    reorg = AsyncMock()
    m = Monitor(engine.cfg, engine.store, None, lambda _: {}, engine.event, reorg, engine.health)
    await m.evm(rpc, {"trader": "0x" + "1" * 40})
    assert engine.store.get("cursor:evm:base:trader:0x" + "1" * 40)["number"] == 100
    engine.store.put("cursor:evm:base:trader:0x" + "1" * 40, {"number": 99, "hash": "orphan"})
    await m.evm(rpc, {"trader": "0x" + "1" * 40})
    reorg.assert_awaited_once_with("base", 99)
    assert engine.store.get("cursor:evm:base:trader:0x" + "1" * 40)["number"] == 35


async def test_unrecoverable_gap_is_visible_and_does_not_jump_cursor(engine):
    rpc = AsyncMock()
    rpc.chain = engine.cfg.chain("base")

    async def call(method, params=None):
        return hex(10000) if method == "eth_blockNumber" else {"hash": "canonical"}

    rpc.call.side_effect = call
    engine.store.put("cursor:evm:base:trader:0x" + "1" * 40, {"number": 1, "hash": "canonical"})
    m = Monitor(
        engine.cfg, engine.store, None, lambda _: {}, engine.event, engine.reorg, engine.health
    )
    await m.evm(rpc, {"trader": "0x" + "1" * 40})
    assert engine.store.get("cursor:evm:base:trader:0x" + "1" * 40)["number"] == 1
    assert engine.store.get("health:gap:base")["state"] == "blocked"


async def test_new_wallet_starts_its_own_checkpoint_without_replaying(engine):
    rpc = AsyncMock()
    rpc.chain = engine.cfg.chain("base")

    async def call(method, params=None):
        if method == "eth_blockNumber":
            return hex(102)
        if method == "eth_getBlockByNumber":
            return {"hash": "canonical", "timestamp": hex(int(now()))}
        if method == "eth_getLogs":
            return []
        raise AssertionError(method)

    rpc.call.side_effect = call
    emitted = AsyncMock()
    monitor = Monitor(
        engine.cfg, engine.store, None, lambda _: {}, emitted, engine.reorg, engine.health
    )
    old, new = "0x" + "1" * 40, "0x" + "2" * 40
    engine.store.put("cursor:evm:base:old:" + old, {"number": 99, "hash": "canonical"})
    await monitor.evm(rpc, {"old": old, "new": new})
    assert engine.store.get("cursor:evm:base:old:" + old)["number"] == 100
    assert engine.store.get("cursor:evm:base:new:" + new)["number"] == 100
    emitted.assert_not_called()
