"""Matched inventory cycles, net of fees; uncertainty is clustered by token."""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from statistics import mean, stdev

from .domain import D, Fill, now

# One-sided 95% Student t critical values, conservatively rounded up.
T95 = {
    1: 6.314,
    2: 2.920,
    3: 2.354,
    4: 2.132,
    5: 2.016,
    6: 1.944,
    7: 1.895,
    8: 1.860,
    9: 1.834,
    10: 1.813,
    15: 1.754,
    20: 1.725,
    30: 1.698,
    60: 1.671,
    120: 1.658,
}


def rank(fills: list[Fill], *, complete_30d: bool, at: float | None = None) -> dict:
    at = now() if at is None else at
    cutoff = at - 30 * 86400
    grouped = defaultdict(list)
    for fill in {f.id: f for f in fills if f.timestamp <= at}.values():
        grouped[(fill.chain, fill.token)].append(fill)
    completed = []
    excluded = []
    for (chain, token), rows in grouped.items():
        rows.sort(key=lambda f: (f.timestamp, f.id))
        if any(f.side in ("transfer", "airdrop") for f in rows):
            excluded.append(f"{chain}:{token}: transfer/airdrop contaminates inventory")
            continue
        if any(f.usd is None or f.fee_usd is None for f in rows):
            excluded.append(f"{chain}:{token}: unknown USD cost or fees")
            continue
        quantity = D(0)
        cost = D(0)
        cycle_cost = D(0)
        pnl = D(0)
        cycles = []
        invalid = False
        for f in rows:
            if f.side == "buy":
                quantity += f.quantity
                cost += f.usd + f.fee_usd
                cycle_cost += f.usd + f.fee_usd
            else:
                if f.quantity > quantity or not quantity:
                    invalid = True
                    break
                matched = cost * f.quantity / quantity
                quantity -= f.quantity
                cost -= matched
                pnl += f.usd - f.fee_usd - matched
                if quantity == 0:
                    if f.timestamp >= cutoff:
                        cycles.append(
                            dict(
                                token=f"{chain}:{token}",
                                cost=cycle_cost,
                                pnl=pnl,
                                closed=f.timestamp,
                            )
                        )
                    cycle_cost = pnl = D(0)
        if invalid:
            excluded.append(f"{chain}:{token}: sell exceeds known purchased inventory")
        else:
            completed.extend(cycles)
    token_cost = defaultdict(Decimal)
    token_pnl = defaultdict(Decimal)
    day_pnl = defaultdict(Decimal)
    for c in completed:
        token_cost[c["token"]] += c["cost"]
        token_pnl[c["token"]] += c["pnl"]
        day = datetime.fromtimestamp(c["closed"], timezone.utc).date().isoformat()
        day_pnl[day] += c["pnl"]
    returns = [float(token_pnl[t] / c) for t, c in token_cost.items() if c > 0]
    n = len(returns)
    confidence = None
    if n >= 2:
        df = n - 1
        critical = T95[max(k for k in T95 if k <= min(df, 120))]
        confidence = mean(returns) - critical * stdev(returns) / math.sqrt(n)
    net = sum(token_pnl.values(), D(0))
    total_cost = sum(token_cost.values(), D(0))
    largest = max(token_pnl.values(), default=D(0))
    reasons = []
    if len(completed) < 20:
        reasons.append("fewer than 20 completed positions")
    if n < 5:
        reasons.append("fewer than five tokens")
    if len(day_pnl) < 3:
        reasons.append("fewer than three trading days")
    if net <= 0:
        reasons.append("nonpositive net return")
    if net - max(largest, D(0)) <= 0:
        reasons.append("not profitable without largest winning token")
    if not complete_30d:
        reasons.append("30-day history coverage unverified")
    if confidence is None or confidence <= 0:
        reasons.append("nonpositive token-cluster confidence bound")
    return dict(
        eligible=not reasons,
        score=confidence,
        completed=len(completed),
        tokens=n,
        days=len(day_pnl),
        positive_days=sum(x > 0 for x in day_pnl.values()),
        net_usd=str(net),
        net_return=str(net / total_cost if total_cost else 0),
        without_best_usd=str(net - max(largest, D(0))),
        largest_profit_share=str(largest / net) if net > 0 else None,
        complete_30d=complete_30d,
        reasons=reasons,
        excluded=excluded,
        token_pnl={t: str(p) for t, p in token_pnl.items()},
        evaluated_at=at,
        method="one-sided 95% Student-t lower bound on token-group net returns",
    )
