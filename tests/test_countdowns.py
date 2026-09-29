"""The countdowns skill: add, list, drop, FACTS, the heads-up nudges.

DESTRUCTIVE: truncates countdowns.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _settings import isolated

from sloane.skills import Registry, SkillContext
from sloane.skills.countdowns import Countdowns

FAILURES: list[str] = []
DEN = ZoneInfo("America/Denver")


def check(label: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{label}\n     got: {got!r}\n    want: {want!r}")


async def integration() -> None:
    from sloane.memory.store import Store

    config = isolated(database_url=os.environ["DATABASE_URL"], timezone="America/Denver")
    now = {"at": datetime(2026, 9, 24, 9, 0, tzinfo=DEN)}  # a Thursday
    async with Store(config) as store:
        await store._exec("truncate countdowns")
        ctx = SkillContext(store=store, config=config, clock=lambda: now["at"])
        skill = Countdowns(ctx)
        reg = Registry([skill], ctx)

        async def cmd(rest):
            return (await reg.command("countdown", rest)).speech

        check("none yet", await cmd(""), "No countdowns set.")
        check("add with a month name", await cmd("graduation may 22"),
              "Counting down to graduation: Sat May 22, 2027, 240 days away.")
        check("add with 'on'", await cmd("DECA districts on oct 1"),
              "Counting down to DECA districts: Thu Oct 1, 7 days away.")
        check("add tomorrow", await cmd("homecoming tomorrow"), "Counting down to homecoming: tomorrow.")
        check("no date", await cmd("prom"), "I couldn't tell the date.")
        check("no name", await cmd("oct 3"), "What are we counting down to?")
        check("a past year", await cmd("old thing 2025-01-01"), "Wed Jan 1, 2025 has already passed.")
        check("the list leads with the soonest", await cmd(""),
              "Homecoming is tomorrow, and 2 more after it.")

        check("facts", await skill.facts(), [
            "- COUNTDOWN homecoming: Fri Sep 25, 2026 (tomorrow)",
            "- COUNTDOWN DECA districts: Thu Oct 1, 2026 (7 days away)",
            "- COUNTDOWN graduation: Sat May 22, 2027 (240 days away)",
        ])
        nudges = await skill.nudges()
        check("heads-up a week out and the day before", [n.text for n in nudges],
              ["📅 Tomorrow: homecoming.", "📅 One week until DECA districts (Thu Oct 1)."])
        check("keys carry the stage so each is said once", len({n.key for n in nudges}), 2)

        check("how many days until", (await reg.route("How many days until graduation?")).speech,
              "240 days until graduation, on Sat May 22, 2027.")
        check("how long until, partial name", (await reg.route("how long until districts")).speech,
              "7 days until DECA districts, on Thu Oct 1.")
        check("not one of ours: the agent answers", await reg.route("how long until the essay is due?"), None)

        now["at"] += timedelta(days=1)
        check("the day itself", (await reg.route("how many days until homecoming")).speech, "Homecoming is today.")
        check("and a day-of nudge", [n.text for n in await skill.nudges()], ["📅 Today: homecoming."])
        now["at"] += timedelta(days=1)
        check("a passed countdown drops out", len(await skill.facts()), 2)

        check("drop", await cmd("drop 1"), "Dropped the countdown to DECA districts.")
        check("drop out of range", await cmd("drop 5"), "Use /countdown drop with a number from /countdown.")
        check("panel", (await skill.panel())["lines"], ["graduation: 238 days (Sat May 22, 2027)"])
        item = (await skill.panel())["items"][0]
        check("with the day it was set, for the control room's bar",
              (item["name"], item["days"], item["created_on"]),
              ("graduation", 238, datetime.now(DEN).date().isoformat()))
        await cmd("ACT prep today")
        check("his capitals are kept", (await reg.route("how many days until ACT prep")).speech, "ACT prep is today.")
        check("in the listing too", await cmd(""), "ACT prep is today, and 1 more after it.")


if os.environ.get("DATABASE_URL"):
    asyncio.run(integration())

if FAILURES:
    print(f"FAIL ({len(FAILURES)})")
    for f in FAILURES:
        print("  -", f)
    raise SystemExit(1)
print("countdowns: add, list, drop, FACTS, panel and the week/day-before/day-of nudges")
