#!/usr/bin/env python3
"""Exercise a synthetic delayed exit against an isolated copy of existing paper state.

No network access. The source must be a completed backup, and the destination
must not exist. This validates code/accounting compatibility, not real chain evidence.
"""

import argparse
import asyncio
import copy
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select

from fomo_spy.config import Settings
from fomo_spy.db import Cash, Ledger, MonitorState, Position, Store
from fomo_spy.demo import DemoQuotes
from fomo_spy.domain import D, now
from fomo_spy.engine import Engine
from fomo_spy.inventory import SolanaCurrent
from fomo_spy.rpc import Monitor


async def verify(source, destination):
    if destination.exists() or source.resolve() == destination.resolve():
        raise ValueError("use a new isolated destination")
    shutil.copy2(source, destination)
    store = Store(destination)
    cfg = Settings(state_dir=destination.parent, selection_policy="paper_research")
    engine = Engine(cfg, store, DemoQuotes(cfg))
    with store.session() as session:
        position = next(
            p
            for p in session.scalars(select(Position))
            if p.mode == "paper" and p.chain == "solana" and D(p.quantity) > 0
        )
        states = {
            r.key: copy.deepcopy(r.data)
            for r in session.scalars(
                select(MonitorState).where(
                    MonitorState.key.startswith(f"solana:{position.trader}:")
                )
            )
        }
        cash_before = D(session.get(Cash, "paper:solana").balance)
        ledger_before = {r.id for r in session.scalars(select(Ledger))}
    state_key, baseline = next(
        (k, v)
        for k, v in states.items()
        if any(a["mint"] == position.token and a["amount"] > 0 for a in v["accounts"].values())
    )
    _, trader, wallet, program = state_key.split(":")
    slot = max(s["slot"] for s in states.values()) + 1
    owned = {a: v for a, v in baseline["accounts"].items() if v["mint"] == position.token}
    addresses = [wallet, *owned]

    def balances(after):
        return [
            {
                "owner": wallet,
                "mint": position.token,
                "programId": program,
                "accountIndex": addresses.index(a),
                "uiTokenAmount": {
                    "amount": "0" if after else str(v["amount"]),
                    "decimals": v["decimals"],
                },
            }
            for a, v in owned.items()
        ]

    tx = {
        "slot": slot,
        "blockTime": int(now()) - 30,
        "transaction": {"message": {"accountKeys": addresses}},
        "meta": {
            "err": None,
            "fee": 5000,
            "preBalances": [10**9] + [2000000] * len(owned),
            "postBalances": [2 * 10**9 - 5000] + [2000000] * len(owned),
            "preTokenBalances": balances(False),
            "postTokenBalances": balances(True),
        },
    }

    async def call(method, params):
        if method == "getBlockTime":
            return int(now())
        if method == "getFirstAvailableBlock":
            return 0
        if method == "getTokenAccountsByOwner":
            state = states[f"solana:{trader}:{wallet}:{params[1]['programId']}"]
            return {
                "context": {"slot": slot},
                "value": [
                    {
                        "pubkey": a,
                        "account": {
                            "owner": v["program"],
                            "data": {
                                "parsed": {
                                    "info": {
                                        "owner": wallet,
                                        "mint": v["mint"],
                                        "tokenAmount": {
                                            "amount": "0" if a in owned else str(v["amount"]),
                                            "decimals": v["decimals"],
                                        },
                                    }
                                }
                            },
                        },
                    }
                    for a, v in state["accounts"].items()
                ],
            }
        if method == "getSignaturesForAddress":
            return (
                [{"signature": "isolated-synthetic-sale", "slot": slot}]
                if params[0] in addresses
                else []
            ) + [
                {"signature": "isolated-boundary", "slot": min(s["slot"] for s in states.values())}
            ]
        if method == "getTransaction":
            return tx
        if method == "getBlock":
            return {
                "blockhash": "isolated-synthetic-block",
                "signatures": ["isolated-synthetic-sale"],
            }
        raise AssertionError(method)

    rpc = SimpleNamespace(chain=cfg.chain("solana"), call=call)
    monitor = Monitor(cfg, store, None, lambda _: {}, engine.event, engine.reorg, engine.health)
    for _ in range(100):
        await SolanaCurrent(monitor).scan(rpc, trader, wallet)
        with store.session() as session:
            if D(session.get(Position, position.id).quantity) == 0:
                break
    restarted = Engine(cfg, store, engine.quotes)
    monitor.emit = restarted.event
    await SolanaCurrent(monitor).scan(rpc, trader, wallet)
    with store.session() as session:
        assert D(session.get(Position, position.id).quantity) == 0
        new = [r for r in session.scalars(select(Ledger)) if r.id not in ledger_before]
        assert len(new) == 1 and new[0].details["recovery"]["delayed"]
        assert D(session.get(Cash, "paper:solana").balance) == cash_before + D(new[0].cash_delta)
    store.close()
    print(
        json.dumps(
            {
                "isolated_synthetic_recovery": "passed",
                "duplicate_prevention": "passed",
                "accounting": "reconciled",
                "real_history_validated": False,
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    asyncio.run(verify(args.source, args.destination))
