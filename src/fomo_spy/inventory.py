"""Reconciled current Solana inventory; no claims about pre-activation history."""

from __future__ import annotations

import copy
from collections import defaultdict

from sqlalchemy import select

from .db import MonitorState, Position
from .domain import D, now
from .normalization import WSOL, solana_movements
from .providers import Unavailable

TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"


def keys(tx):
    return [
        row["pubkey"] if isinstance(row, dict) else row
        for row in tx["transaction"]["message"]["accountKeys"]
    ]


def accounts(tx, wallet, field):
    addresses = keys(tx)
    return {
        addresses[row["accountIndex"]]: {
            "mint": row["mint"],
            "amount": int(row["uiTokenAmount"]["amount"]),
            "decimals": row["uiTokenAmount"]["decimals"],
            "program": row.get("programId", TOKEN),
        }
        for row in tx["meta"].get(field, [])
        if row.get("owner") == wallet
    }


class SolanaCurrent:
    def __init__(self, monitor):
        self.monitor, self.store = monitor, monitor.store

    def save(self, key, data):
        with self.store.session() as session:
            session.merge(MonitorState(key=key, data=data))

    async def snapshot(self, rpc, wallet, program):
        result = await rpc.call(
            "getTokenAccountsByOwner",
            [wallet, {"programId": program}, {"encoding": "jsonParsed", "commitment": "finalized"}],
        )
        values = {}
        for row in result["value"]:
            info = row["account"]["data"]["parsed"]["info"]
            if info["owner"] != wallet or row["account"]["owner"] != program:
                raise Unavailable("token account snapshot owner/program mismatch")
            values[row["pubkey"]] = {
                "mint": info["mint"],
                "amount": int(info["tokenAmount"]["amount"]),
                "decimals": info["tokenAmount"]["decimals"],
                "program": program,
            }
        return int(result["context"]["slot"]), values

    async def signatures(self, rpc, address, lower, upper):
        before, seen, result = None, set(), {}
        for _ in range(10):
            options = {"limit": 100, "commitment": "finalized"}
            if before:
                options["before"] = before
            page = await rpc.call("getSignaturesForAddress", [address, options])
            if not page:
                return result
            for row in page:
                if row["signature"] in seen:
                    raise Unavailable("repeated Solana monitoring signature page")
                seen.add(row["signature"])
                if row["slot"] <= lower:
                    return result
                if row["slot"] <= upper and not row.get("err"):
                    result[row["signature"]] = row
            before = page[-1]["signature"]
        raise Unavailable("Solana monitoring gap exceeds 1000 signatures; reconciliation required")

    async def transaction(self, rpc, signature):
        from .rpc import SOLANA_READ_VERSION

        tx = await rpc.call(
            "getTransaction",
            [
                signature,
                {
                    "encoding": "jsonParsed",
                    "commitment": "finalized",
                    "maxSupportedTransactionVersion": SOLANA_READ_VERSION,
                },
            ],
        )
        if not tx or tx.get("blockTime") is None or tx.get("meta") is None:
            raise Unavailable("finalized Solana transaction unavailable")
        return tx

    async def scan(self, rpc, trader, wallet):
        self.monitor.new_activation(rpc.chain.name, trader)
        errors = []
        for program in (TOKEN, TOKEN_2022):
            try:
                await self.scan_program(rpc, trader, wallet, program)
            except Unavailable as exc:
                errors.append(exc)
        if errors:
            raise errors[0]

    async def scan_program(self, rpc, trader, wallet, program):
        chain = rpc.chain.name
        key = f"solana:{trader}:{wallet}:{program}"
        with self.store.session() as session:
            row = session.get(MonitorState, key)
            state = copy.deepcopy(row.data) if row else None
            positions = list(
                session.scalars(
                    select(Position).where(
                        Position.mode == self.monitor.cfg.mode,
                        Position.chain == chain,
                        Position.trader == trader,
                    )
                )
            )
            open_mints = {p.token for p in positions if D(p.quantity) > 0}
        slot, snapshot = await self.snapshot(rpc, wallet, program)
        activation = self.store.get(f"monitor_epoch:{chain}:{trader}")
        if state and state.get("activation") != activation and not open_mints:
            state = None
        if state is None:
            self.save(
                key,
                {
                    "slot": slot,
                    "anchor": slot,
                    "accounts": snapshot,
                    "at": now(),
                    "activation": activation,
                },
            )
            self.monitor.ready(chain, trader, slot)
            return
        if slot < state["slot"]:
            raise Unavailable("finalized Solana snapshot moved backwards")
        if slot == state["slot"]:
            if now() - state["at"] > 120:
                raise Unavailable("finalized Solana snapshot has not advanced for two minutes")
            self.monitor.ready(chain, trader, state["anchor"])
            return
        signatures = await self.signatures(rpc, wallet, state["slot"], slot)
        transactions = {sig: await self.transaction(rpc, sig) for sig in signatures}
        relevant = set(open_mints)
        discovered = dict(state["accounts"])
        discovered.update(snapshot)
        for tx in transactions.values():
            for field in ("preTokenBalances", "postTokenBalances"):
                discovered.update(accounts(tx, wallet, field))
            relevant.update(
                m.signal.token
                for m in solana_movements(
                    tx, trader, wallet, "discovery", rpc.chain, self.monitor.started
                )
                if m.signal.side in ("buy", "sell")
            )
        relevant -= {rpc.chain.settlement, WSOL}
        relevant &= {a["mint"] for a in discovered.values() if a["program"] == program}
        # Token-account transfers need not mention the owner's wallet. Discover
        # every known account of a traded/copied mint, including closed accounts.
        for address, data in discovered.items():
            if data["mint"] in relevant and data["program"] == program:
                rows = await self.signatures(rpc, address, state["slot"], slot)
                for sig in rows.keys() - transactions.keys():
                    transactions[sig] = await self.transaction(rpc, sig)
        by_slot = defaultdict(list)
        for sig, tx in transactions.items():
            by_slot[tx["slot"]].append((sig, tx))
        ordered = []
        for height in sorted(by_slot):
            block = await rpc.call(
                "getBlock",
                [
                    height,
                    {
                        "commitment": "finalized",
                        "transactionDetails": "signatures",
                        "rewards": False,
                        "maxSupportedTransactionVersion": 1,
                    },
                ],
            )
            if not block or not block.get("blockhash") or not block.get("signatures"):
                raise Unavailable("canonical Solana block signatures unavailable")
            index = {sig: i for i, sig in enumerate(block["signatures"])}
            if any(sig not in index for sig, _ in by_slot[height]):
                raise Unavailable("Solana transaction absent from finalized block")
            ordered.extend(
                (sig, tx, block["blockhash"])
                for sig, tx in sorted(by_slot[height], key=lambda pair: index[pair[0]])
            )
        working = copy.deepcopy(state["accounts"])
        pending, gaps = [], set()
        for signature, tx, block_hash in ordered:
            pre = accounts(tx, wallet, "preTokenBalances")
            post = accounts(tx, wallet, "postTokenBalances")
            movements = solana_movements(
                tx, trader, wallet, signature, rpc.chain, self.monitor.started
            )
            totals = {
                mint: sum(a["amount"] for a in working.values() if a["mint"] == mint)
                for mint in relevant
            }
            for address in pre.keys() | post.keys():
                info = pre.get(address, post.get(address))
                mint = info["mint"]
                if mint not in relevant:
                    continue
                if info["program"] != program:
                    gaps.add(mint)
                    continue
                if working.get(address, {}).get("amount", 0) != pre.get(address, {}).get(
                    "amount", 0
                ):
                    gaps.add(mint)
                if address in post:
                    working[address] = post[address]
                else:
                    working.pop(address, None)
            for movement in movements:
                signal = movement.signal
                if signal.token not in relevant:
                    continue
                before = D(totals.get(signal.token, 0)) / 10**signal.token_decimals
                after = (
                    D(sum(a["amount"] for a in working.values() if a["mint"] == signal.token))
                    / 10**signal.token_decimals
                )
                signal.source_before, signal.source_after, signal.block_hash = (
                    before,
                    after,
                    block_hash,
                )
                if abs(after - before) != signal.quantity:
                    gaps.add(signal.token)
                pending.append(movement)
        for mint in relevant:
            expected = {
                k: v["amount"] for k, v in snapshot.items() if v["mint"] == mint and v["amount"]
            }
            actual = {
                k: v["amount"] for k, v in working.items() if v["mint"] == mint and v["amount"]
            }
            if expected != actual:
                gaps.add(mint)
        if gaps:
            with self.store.session() as session:
                for position in session.scalars(
                    select(Position).where(
                        Position.mode == self.monitor.cfg.mode,
                        Position.chain == chain,
                        Position.trader == trader,
                    )
                ):
                    if position.token in gaps and D(position.quantity) > 0:
                        position.needs_reconcile = True
            self.monitor.health(
                f"inventory:{chain}:{trader}",
                {
                    "state": "reconciliation required",
                    "tokens": sorted(gaps),
                    "reason": "current token accounts do not reconcile or token program is unsupported",
                    "at": now(),
                },
            )
        self.monitor.ready(chain, trader, state["anchor"])
        for movement in pending:
            if movement.signal.token in gaps:
                movement.signal.source_before = movement.signal.source_after = None
                # Keep an explicit rejected event without inventing inventory.
            await self.monitor.emit_movement(movement)
        self.save(
            key,
            {
                "slot": slot,
                "anchor": state["anchor"],
                "accounts": snapshot,
                "at": now(),
                "activation": activation,
            },
        )
        self.store.put(
            "last_chain_data", {"at": now(), "chain": chain, "wallet": wallet, "slot": slot}
        )
