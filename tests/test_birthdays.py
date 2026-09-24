"""Birthdays: on people, the next one computed (Feb 29 too), heads-up nudges.

DESTRUCTIVE: truncates people (and so commitments' person links).
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _settings import isolated

from sloane.skills import Registry, SkillContext
from sloane.skills.birthdays import Birthdays, next_birthday

FAILURES: list[str] = []
DEN = ZoneInfo("America/Denver")
TODAY = date(2026, 9, 24)


def check(label: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{label}\n     got: {got!r}\n    want: {want!r}")


check("later this year", next_birthday(10, 3, TODAY), date(2026, 10, 3))
check("today counts", next_birthday(9, 24, TODAY), TODAY)
check("passed: next year", next_birthday(3, 3, TODAY), date(2027, 3, 3))
check("Feb 29 in a common year is Feb 28", next_birthday(2, 29, TODAY), date(2027, 2, 28))
check("Feb 29 in a leap year", next_birthday(2, 29, date(2027, 3, 1)), date(2028, 2, 29))


async def integration() -> None:
    from sloane.memory.store import Store

    config = isolated(database_url=os.environ["DATABASE_URL"], timezone="America/Denver")
    now = {"at": datetime(2026, 9, 24, 9, 0, tzinfo=DEN)}
    async with Store(config) as store:
        await store._exec("truncate people cascade")
        keegan = await store.person_id("Keegan Hart")  # already known from a promise
        ctx = SkillContext(store=store, config=config, clock=lambda: now["at"])
        skill = Birthdays(ctx)
        reg = Registry([skill], ctx)

        async def cmd(rest=""):
            return (await reg.command("birthday", rest)).speech

        check("none yet", await cmd(), "I don't know anyone's birthday yet.")
        check("set on an existing person by first name", await cmd("Keegan oct 1"),
              "Got it: Keegan Hart's birthday is October 1, in 7 days.")
        row = await store._one("select birth_month, birth_day from people where id = %s", (keegan,))
        check("stored on the same person", (row["birth_month"], row["birth_day"]), (10, 1))
        check("plain words make a new person", (await reg.route("Maya's birthday is March 3")).speech,
              "Got it: Maya's birthday is March 3, in 160 days.")
        check("ask", (await reg.route("when is Keegan's birthday?")).speech,
              "Keegan Hart's birthday is October 1, in 7 days.")
        check("ask about someone unknown: the agent's", await reg.route("when's Zed's birthday"), None)
        check("not a birthday sentence", await reg.route("Maya's birthday is going to be fun"), None)
        check("list", await cmd(), "Next up: Keegan Hart, in 7 days on Thu Oct 1.")
        check("FACTS: only the next two weeks", await skill.facts(), ["- BIRTHDAY Keegan Hart: Thu Oct 1 (in 7 days)"])
        check("a week out", [n.text for n in await skill.nudges()],
              ["🎂 Keegan Hart's birthday is a week from today (Thursday, Oct 1)."])
        now["at"] = datetime(2026, 9, 30, 12, 0, tzinfo=DEN)
        check("the day before, not before 5 PM", await skill.nudges(), [])
        now["at"] = datetime(2026, 9, 30, 18, 0, tzinfo=DEN)
        check("the evening before", [n.text for n in await skill.nudges()], ["🎂 Keegan Hart's birthday is tomorrow."])
        now["at"] = datetime(2026, 10, 1, 7, 30, tzinfo=DEN)
        nudges = await skill.nudges()
        check("the morning of", [n.text for n in nudges], ["🎂 It's Keegan Hart's birthday today."])
        check("keys carry the year and stage", nudges[0].key.endswith(":2026-10-01:0"), True)
        now["at"] += timedelta(days=1)
        check("then it's next year's", (await skill.facts()), [])
        check("forget", await cmd("keegan forget"), "Forgot Keegan Hart's birthday.")
        check("panel", (await skill.panel()), None)


if os.environ.get("DATABASE_URL"):
    asyncio.run(integration())

if FAILURES:
    print(f"FAIL ({len(FAILURES)})")
    for f in FAILURES:
        print("  -", f)
    raise SystemExit(1)
print("birthdays: on people, next occurrence (Feb 29 too), heads-up nudges, FACTS")
