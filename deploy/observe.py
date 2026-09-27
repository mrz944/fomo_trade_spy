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


def accounting(database):
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
            "integrity": integrity,
            "cash": str(cash),
            "ledger_cash_delta": str(delta),
            "implied_initial_capital": str(cash - delta),
            "orders": dict(
                db.execute("SELECT state,count(*) FROM orders WHERE mode='paper' GROUP BY state")
            ),
            "reconciliation_flags": db.execute(
                "SELECT count(*) FROM positions WHERE needs_reconcile=1"
            ).fetchone()[0],
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
                    )
                }
            )
            workflow = status.get("workflow", {})
            sample["workflow"] = {k: v for k, v in workflow.items() if k != "capabilities"}
            sample["health"] = status.get("health")
            sample["accounting"] = accounting(args.database)
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
                "fresh_paper_execution_observed": any(
                    r.get("workflow", {}).get("observed_fills", 0) > 0 for r in rows
                ),
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
