"""Merge a nightly backup back into the database.

    python scripts/restore_backup.py /var/lib/sloane/models/backups/sloane-2026-09-28.json
    python scripts/restore_backup.py FILE --tables commitments,state --apply

Dry run by default: it says what it would bring back and changes nothing.
With --apply it inserts rows that are missing now and leaves every existing
row alone, so running it can recover lost data but never undo newer changes.
In Docker:

    docker compose run --rm sloane python scripts/restore_backup.py \\
        /var/lib/sloane/models/backups/sloane-YYYY-MM-DD.json --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sloane.config import settings
from sloane.memory.store import Store


async def main(path: str, tables: list[str] | None, apply: bool) -> int:
    data = json.loads(Path(path).read_text())["tables"]
    chosen = tables or [t for t in Store.BACKUP_TABLES if t in data]
    unknown = [t for t in chosen if t not in Store.BACKUP_TABLES]
    if unknown:
        print(f"not backed-up tables: {', '.join(unknown)}")
        return 2
    async with Store(settings()) as store:
        # Dependencies first (commitments point at people, habit_log at habits):
        # BACKUP_TABLES is kept in that order.
        order = sorted(chosen, key=Store.BACKUP_TABLES.index)
        for table in order:
            rows = data.get(table, [])
            if not apply:
                print(f"{table}: {len(rows)} rows in the backup (dry run; --apply to merge)")
                continue
            added = await store.restore_rows(table, rows)
            print(f"{table}: {added} restored, {len(rows) - added} already present")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("file")
    parser.add_argument("--tables", help="comma-separated; default: all in the file")
    parser.add_argument("--apply", action="store_true", help="actually write (default: dry run)")
    args = parser.parse_args()
    tables = [t.strip() for t in args.tables.split(",")] if args.tables else None
    sys.exit(asyncio.run(main(args.file, tables, args.apply)))
