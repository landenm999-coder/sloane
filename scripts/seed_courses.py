#!/usr/bin/env python3
"""Seed tier 4 with Landen's real semester schedule.

Canvas knows the course ids; it does not know which period a class sits in or
who teaches it. That comes from here, and the sync links the two by name, so
"what's due Friday" can answer "Stat p. 214, Austin, period 2" rather than
echoing whatever the course is called upstream.

Re-running is safe: courses upsert on (semester, period).

    python scripts/seed_courses.py            # Sem 1, from the build plan
    python scripts/seed_courses.py --list     # show what is stored now
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sloane.config import settings as load_settings
from sloane.memory.store import Store

# period, name, teacher. None means a free period -- recorded, not skipped, so
# she knows 4th is off rather than merely having no record of it.
SEMESTER_1: list[tuple[int, str, str | None]] = [
    (1, "Jewelry I", "Orlando"),
    (2, "Stat Reasoning", "Austin"),
    # Seeded under the name the school uses, not his shorthand: the sync
    # links courses by name, and "CO" is too short to safely prefix-match
    # "Colorado". His shorthand lives in tier 1 state instead.
    (3, "Colorado History", "Allen"),
    (4, "Off period", None),
    (5, "American Tapestry", "Olsen"),
    (6, "Physics", "Babcock"),
    (7, "Off period", None),
]


async def main(show_only: bool) -> int:
    config = load_settings()
    if not config.database_url:
        print("DATABASE_URL is unset.", file=sys.stderr)
        return 1

    async with Store(config) as store:
        if not show_only:
            for period, name, teacher in SEMESTER_1:
                await store.upsert_course(
                    name=name, period=period, teacher=teacher, semester="S1"
                )
            print(f"seeded {len(SEMESTER_1)} periods for S1")

        rows = await store.courses()

    print(f"{'period':>6}  {'course':22} {'teacher':12} linked")
    for row in rows:
        period = row["period"] if row["period"] is not None else "-"
        linked = "yes" if row.get("external_id") else "no"
        print(f"{period:>6}  {row['name']:22} {(row['teacher'] or '-'):12} {linked}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main("--list" in sys.argv)))
