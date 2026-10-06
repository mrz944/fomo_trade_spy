#!/usr/bin/env python3
"""Upgrade an isolated backup copy and compare every existing record semantically."""

import argparse
import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

from fomo_spy.db import Store


def records(path):
    # Inputs are completed backups or a fully closed migrated copy. Immutable
    # reads avoid trying to create WAL/SHM files beside a read-only bind mount.
    with sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True) as database:
        database.row_factory = sqlite3.Row
        database.execute("BEGIN")
        assert database.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        tables = [
            r[0]
            for r in database.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name != 'alembic_version'"
            )
        ]
        result = {}
        for table in tables:
            rows = []
            for record in database.execute('SELECT * FROM "' + table.replace('"', '""') + '"'):
                row = dict(record)
                if table == "positions":
                    assert row.pop("selection_policy", "verified") == "verified"
                for field in ("data", "details", "value"):
                    if field in row:
                        row[field] = json.loads(row[field])
                        if table in ("orders", "ledger"):
                            assert row[field].pop("selection_policy", "verified") == "verified"
                rows.append(json.dumps(row, sort_keys=True))
            result[table] = {
                "rows": len(rows),
                "sha256": hashlib.sha256(json.dumps(sorted(rows)).encode()).hexdigest(),
            }
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Completed SQLite online backup")
    parser.add_argument("copy", type=Path, help="New isolated destination; never production")
    args = parser.parse_args()
    if args.copy.exists():
        parser.error("copy already exists; choose a new isolated path")
    before = records(args.source)
    shutil.copy2(args.source, args.copy)
    store = Store(args.copy)
    store.close()
    after = records(args.copy)
    assert all(before[table] == after[table] for table in before), "existing records changed"
    with sqlite3.connect(args.copy) as database:
        revision = database.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        assert revision == "0004"
    print(
        json.dumps(
            {
                "migration": revision,
                "integrity": "ok",
                "existing_records_preserved": True,
                "tables": after,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
