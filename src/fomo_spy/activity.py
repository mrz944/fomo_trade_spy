"""Read-only observations. These records can never become trading signals."""

from sqlalchemy import delete, select

from .db import CoverageGap, EvidenceFill, Observation
from .domain import now


def observe(
    store,
    key,
    *,
    kind,
    trader="",
    chain="",
    side="",
    token="",
    timestamp=None,
    status,
    reason,
    received=None,
):
    received = now() if received is None else received
    data = dict(
        kind=kind,
        trader=trader,
        chain=chain,
        side=side,
        token=token,
        timestamp=timestamp,
        received=received,
        status=status,
        reason=reason,
    )
    with store.session() as session:
        if session.get(Observation, key):
            return False
        session.add(Observation(key=key, received=received, data=data))
        session.flush()
        # Bound feed storage, independently of the immutable trading audit ledger.
        retained = select(Observation.key).order_by(Observation.received.desc()).limit(2000)
        session.execute(delete(Observation).where(Observation.key.not_in(retained)))
    return True


def timeline(store, events):
    rows = [
        {
            **e.data,
            "kind": "trade decision",
            "status": e.status,
            "reason": e.reason,
            "received": e.received,
        }
        for e in events
    ]
    with store.session() as session:
        rows.extend(
            r.data
            for r in session.scalars(
                select(Observation).order_by(Observation.received.desc()).limit(100)
            )
        )
        # Existing historical receipts are visible immediately after upgrade.
        for row in session.scalars(
            select(EvidenceFill).order_by(EvidenceFill.timestamp.desc()).limit(20)
        ):
            fill = row.data["fill"]
            rows.append(
                {
                    **fill,
                    "kind": "historical evidence",
                    "received": row.data.get("received", fill["timestamp"]),
                    "status": "recorded; not copied",
                    "reason": "Historical reconstruction never opens positions",
                }
            )
        # Show a bounded current blocker per chain, rather than 350 repeated rows.
        gaps = {}
        for gap in session.scalars(select(CoverageGap)):
            if not gap.data.get("reason"):
                continue
            parts = gap.key.split(":", 2)
            if len(parts) != 3:
                continue
            trader, chain, _ = parts
            priority = (gap.data["reason"] != "wallet unavailable", gap.data.get("at", 0))
            previous = gaps.get(chain)
            if previous is None or priority > (
                previous["reason"] != "wallet unavailable",
                previous["received"],
            ):
                gaps[chain] = dict(
                    kind="history check",
                    trader=trader,
                    chain=chain,
                    side="",
                    token="",
                    timestamp=None,
                    received=gap.data.get("at", 0),
                    status="blocked",
                    reason=gap.data["reason"],
                )
        rows.extend(gaps.values())
    return sorted(rows, key=lambda r: r["received"], reverse=True)[:100]
