"""Paper-only research admission. Never writes verified ranking/coverage claims."""

from __future__ import annotations

import re
from decimal import InvalidOperation

import base58
from sqlalchemy import select

from .db import ResearchSelection
from .domain import D, now


def valid_wallet(wallet, kind):
    if not isinstance(wallet, str):
        return False
    if kind == "evm":
        return bool(re.fullmatch(r"0x[0-9a-fA-F]{40}", wallet))
    try:
        return len(base58.b58decode(wallet)) == 32
    except (ValueError, TypeError):
        return False


class ResearchPolicy:
    def __init__(self, cfg, store):
        self.cfg, self.store = cfg, store

    def records(self):
        with self.store.session() as session:
            return {row.key: row.data for row in session.scalars(select(ResearchSelection))}

    def available_chains(self, trader, at):
        result = []
        for chain in self.cfg.chains:
            cap = self.store.get("current_capability:" + chain.name, {})
            wallet = trader.get("wallets", {}).get(chain.kind == "solana" and "solana" or "evm")
            if (
                chain.enabled
                and cap.get("observe_ready")
                and cap.get("at", 0) >= at - 7200
                and valid_wallet(wallet, chain.kind)
            ):
                result.append(chain.name)
        return result

    def refresh(self, at=None):
        at = now() if at is None else at
        board = self.store.get("current_leaderboard", {})
        previous = self.records()
        excluded = set(self.store.get("excluded", []))
        candidates = []
        if board.get("expires", 0) > at:
            for uid in board.get("traders", []):
                trader = self.store.get("trader:" + uid, {})
                try:
                    pnl = D(str(trader.get("pnlUsd")))
                    positive = pnl.is_finite() and pnl > 0
                except (InvalidOperation, TypeError, ValueError):
                    positive = False
                chains = self.available_chains(trader, at)
                if uid in excluded or not positive or not chains:
                    continue
                activity = max(
                    (
                        self.store.get(f"research_activity:{uid}:{chain}", {}).get("at", 0)
                        for chain in chains
                    ),
                    default=0,
                )
                old = previous.get(uid, {})
                retained = (
                    old.get("selected")
                    and at - old.get("activated_at", 0) < self.cfg.research_min_dwell
                )
                candidates.append(
                    dict(
                        trader=uid,
                        chains=chains,
                        rank=trader.get("rank", 10000),
                        activity_at=activity,
                        retained=bool(retained),
                        handle=trader.get("handle", uid),
                        reported_pnl_usd=str(pnl),
                    )
                )
        candidates.sort(
            key=lambda r: (not r["retained"], r["activity_at"] < at - 3600, r["rank"], r["trader"])
        )
        chosen = {row["trader"]: row for row in candidates[: self.cfg.follow]}
        with self.store.session() as session:
            for uid in previous.keys() | chosen.keys():
                old = previous.get(uid, {})
                row = chosen.get(uid)
                data = dict(
                    old,
                    selected=False,
                    checked_at=at,
                    reason="not in current usable research selection",
                )
                if row:
                    active = old.get("activated_at", at) if old.get("selected") else at
                    pair_times = {
                        chain: old.get("pair_activated", {}).get(chain, at)
                        if old.get("selected")
                        else at
                        for chain in row["chains"]
                    }
                    data.update(
                        row,
                        selected=True,
                        activated_at=active,
                        pair_activated=pair_times,
                        expires=board["expires"],
                        policy="paper_research",
                        reason="recently active leaderboard candidate"
                        if row["activity_at"] >= at - 3600
                        else "leaderboard candidate awaiting fresh activity",
                    )
                session.merge(ResearchSelection(key=uid, data=data))
        self.store.put("research_selection_checked", {"at": at})
        return self.selected_records(at)

    def selected_records(self, at=None):
        at = now() if at is None else at
        excluded = set(self.store.get("excluded", []))
        board = self.store.get("current_leaderboard", {})
        return [
            row
            for uid, row in self.records().items()
            if row.get("selected")
            and board.get("expires", 0) > at
            and row.get("expires", 0) > at
            and uid not in excluded
            and uid in board.get("traders", [])
        ]

    def pairs(self):
        pairs = []
        for row in self.selected_records():
            trader = self.store.get("trader:" + row["trader"], {})
            for chain in self.available_chains(trader, now()):
                if chain in row.get("chains", []):
                    pairs.append(
                        dict(
                            trader=row["trader"],
                            chain=chain,
                            activated_at=row["pair_activated"][chain],
                            reason=row["reason"],
                        )
                    )
        return pairs

    def allows(self, sig):
        return any(
            pair["trader"] == sig.trader
            and pair["chain"] == sig.chain
            and sig.timestamp >= pair["activated_at"]
            for pair in self.pairs()
        )
