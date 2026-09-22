#!/usr/bin/env python3
"""Load tier 1 from a markdown file.

Tier 1 is the block that rides in every prompt, so it is worth writing by hand
and keeping in version control rather than hoping she infers it.

Format — `## Heading` sets the category, `- key: value` is a fact:

    ## schedule
    - work.hours: 3-7 PM Mon-Fri
    - class.2: Stat Reasoning, Austin

    ## people
    - person.keegan: friend, sends the DECA deck edits

Re-running is safe: keys upsert, so editing the file and re-running updates in
place rather than duplicating.

    python scripts/seed_state.py state.md
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sloane.config import settings as load_settings
from sloane.memory.store import Store

HEADING = re.compile(r"^#{1,6}\s+(?P<name>.+?)\s*$")
FACT = re.compile(r"^\s*[-*+]\s+(?P<key>[^:]+?)\s*:\s*(?P<value>.+?)\s*$")


def parse_markdown(text: str) -> list[tuple[str, str, str]]:
    """Return (key, value, category) triples in file order."""
    category = "fact"
    facts: list[tuple[str, str, str]] = []
    for line in text.splitlines():
        heading = HEADING.match(line)
        if heading:
            category = heading.group("name").strip().lower()
            continue
        fact = FACT.match(line)
        if fact:
            key = fact.group("key").strip().strip("*_`")
            value = fact.group("value").strip()
            if key and value:
                facts.append((key, value, category))
    return facts


async def main(path: Path) -> int:
    if not path.exists():
        print(f"no such file: {path}", file=sys.stderr)
        return 1

    facts = parse_markdown(path.read_text())
    if not facts:
        print(f"{path} has no `- key: value` lines under any heading.", file=sys.stderr)
        return 1

    config = load_settings()
    if not config.database_url:
        print("DATABASE_URL is unset.", file=sys.stderr)
        return 1

    async with Store(config) as store:
        for key, value, category in facts:
            await store.put_state(key, value, category=category, source=str(path))
        total = len(await store.get_state())

    by_category: dict[str, int] = {}
    for _, _, category in facts:
        by_category[category] = by_category.get(category, 0) + 1
    print(f"seeded {len(facts)} facts from {path}")
    for category, count in sorted(by_category.items()):
        print(f"  {category}: {count}")
    print(f"tier 1 now holds {total} facts")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(asyncio.run(main(Path(sys.argv[1]))))
