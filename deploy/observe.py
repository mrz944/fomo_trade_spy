#!/usr/bin/env python3
"""Read-only 24-hour FOMO observation, run on the deployment host.

No keys, RPC requests, controls, or synthetic events. Restart with the same
output directory to resume the original observation deadline.
"""

import argparse
import json
import os
import sqlite3
import subprocess
import time
from decimal import Decimal
from pathlib import Path


def command(*args):
    result = subprocess.run(args, text=True, capture_output=True, timeout=30, check=True)
    return result.stdout


def accounting(database, since=0):
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=10) as db:
        db.execute("BEGIN")
        integrity = db.execute("PRAGMA quick_check").fetchone()[0]
        cash = sum(
            (Decimal(r[0]) for r in db.execute("SELECT balance FROM cash WHERE id LIKE 'paper:%'")),
            Decimal(0),
        )
        delta = sum(
            (Decimal(r[0]) for r in db.execute("SELECT cash_delta FROM ledger WHERE mode='paper'")),
            Decimal(0),
        )
        return {
            "fresh_source_fills": dict(
                db.execute(
                    "SELECT o.side,count(*) FROM ledger l JOIN orders o ON o.id=l.id "
                    "JOIN events e ON e.key=o.event_key WHERE l.mode='paper' AND l.timestamp>=? "
                    "AND json_extract(e.data,'$.provider')='rpc' "
                    "AND json_extract(e.data,'$.finalized')=1 "
                    "AND coalesce(json_extract(e.data,'$.historical'),0)=0 "
                    "AND coalesce(json_extract(o.data,'$.manual'),0)=0 "
                    "AND coalesce(json_extract(l.details,'$.recovery.delayed'),0)=0 GROUP BY o.side",
                    (since,),
                )
            ),
            "window_fills": dict(
                db.execute(
                    "SELECT json_extract(details,'$.side'),count(*) FROM ledger WHERE mode='paper' AND timestamp>=? GROUP BY 1",
                    (since,),
                )
            ),
            "recovered_exits": db.execute(
                "SELECT count(*) FROM ledger WHERE mode='paper' AND timestamp>=? AND json_extract(details,'$.recovery.delayed')=1 AND json_extract(details,'$.side')='sell'",
                (since,),
            ).fetchone()[0],
            "integrity": integrity,
            "cash": str(cash),
            "ledger_cash_delta": str(delta),
            "implied_initial_capital": str(cash - delta),
            "orders": dict(
                db.execute("SELECT state,count(*) FROM orders WHERE mode='paper' GROUP BY state")
            ),
            "filled_sides": dict(
                db.execute(
                    "SELECT side,count(*) FROM orders WHERE mode='paper' AND state='filled' GROUP BY side"
                )
            ),
            "policy_ledger_rows": dict(
                db.execute(
                    "SELECT coalesce(json_extract(details,'$.selection_policy'),'verified'),count(*) "
                    "FROM ledger WHERE mode='paper' GROUP BY 1"
                )
            ),
            "reconciliation_flags": db.execute(
                "SELECT count(*) FROM positions WHERE needs_reconcile=1"
            ).fetchone()[0],
        }


def observation_metrics(rows):
    successful = [r for r in rows if r.get("ok")]
    first = successful[0] if successful else {}
    last = successful[-1] if successful else {}
    initial = {r["key"]: r.get("checkpoint", 0) for r in first.get("monitoring", [])}
    fills = last.get("accounting", {}).get("fresh_source_fills", {})
    recovered = last.get("accounting", {}).get("recovered_exits", 0)
    buys, exits = fills.get("buy", 0), fills.get("sell", 0)
    return {
        "fresh_buy_fills": buys,
        "fresh_exit_fills": exits,
        "recovered_exit_fills": recovered,
        "fresh_paper_execution_observed": buys + exits > 0,
        "execution_acceptance": "observed"
        if buys and exits
        else "pending qualifying source transactions",
        "checkpoint_advancement": {
            r["key"]: r.get("checkpoint", 0) - initial[r["key"]]
            for r in last.get("monitoring", [])
            if r["key"] in initial
        },
        "monitoring_gap_samples": sum(
            bool(r.get("workflow", {}).get("affected_positions")) for r in successful
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--database", type=Path, default=Path.home() / ".fomo-trade-spy/data/spy.sqlite"
    )
    parser.add_argument("--duration", type=int, default=86400)
    parser.add_argument("--interval", type=int, default=60)
    args = parser.parse_args()
    if args.interval < 1 or args.duration < 1:
        parser.error("duration and interval must be positive")
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
    metadata = args.output / "observation.json"
    if metadata.exists():
        meta = json.loads(metadata.read_text())
    else:
        meta = {
            "started": time.time(),
            "duration": args.duration,
            "interval": args.interval,
            "image": command(
                "podman",
                "inspect",
                "fomo-spy",
                "--format",
                '{{.Image}} {{index .Config.Labels "org.opencontainers.image.revision"}} ',
            ).strip(),
        }
        metadata.write_text(json.dumps(meta, indent=2) + "\n")
    samples = args.output / "samples.jsonl"
    while True:
        sample = {"at": time.time()}
        try:
            status = json.loads(
                command(
                    "podman",
                    "exec",
                    "fomo-spy",
                    "fomo-spy",
                    "status",
                    "--socket",
                    "/run/fomo-spy/daemon.sock",
                )
            )
            sample.update(
                {
                    k: status.get(k)
                    for k in (
                        "mode",
                        "uptime",
                        "paused",
                        "equity_usd",
                        "credits",
                        "selected",
                        "watching",
                        "selection_policy",
                        "policy_performance",
                        "selected_pairs",
                    )
                }
            )
            workflow = status.get("workflow", {})
            sample["workflow"] = {k: v for k, v in workflow.items() if k != "capabilities"}
            sample["health"] = status.get("health")
            sample["accounting"] = accounting(args.database, meta["started"])
            sample["monitoring"] = status.get("monitoring", [])
            sample["service"] = command(
                "systemctl",
                "--user",
                "show",
                "fomo-spy.service",
                "-p",
                "ActiveState",
                "-p",
                "NRestarts",
                "-p",
                "MainPID",
            ).strip()
            sample["ok"] = True
        except Exception as exc:
            sample.update(ok=False, error=type(exc).__name__)
        with samples.open("a") as file:
            file.write(json.dumps(sample) + "\n")
        elapsed = time.time() - meta["started"]
        if elapsed >= meta["duration"]:
            rows = [json.loads(line) for line in samples.read_text().splitlines()]
            summary = {
                "elapsed_seconds": elapsed,
                "samples": len(rows),
                "successful_samples": sum(r["ok"] for r in rows),
                "first": rows[0],
                "last": rows[-1],
                "completed": True,
                **observation_metrics(rows),
                "accounting_errors": [
                    r["at"]
                    for r in rows
                    if r.get("accounting", {}).get("integrity", "ok") != "ok"
                    or Decimal(r.get("accounting", {}).get("implied_initial_capital", "1000"))
                    != Decimal(1000)
                ],
            }
            (args.output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
            return
        time.sleep(min(meta["interval"], meta["duration"] - elapsed))


if __name__ == "__main__":
    main()
