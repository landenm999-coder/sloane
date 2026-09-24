"""Habits: streaks counted in code, marking by rule, the one evening nudge.

DESTRUCTIVE: truncates habits and habit_log.
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
from sloane.skills.habits import Habits, best, streak

FAILURES: list[str] = []
DEN = ZoneInfo("America/Denver")
TODAY = date(2026, 9, 24)


def check(label: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{label}\n     got: {got!r}\n    want: {want!r}")


def days(*offsets: int) -> set[date]:
    return {TODAY - timedelta(days=o) for o in offsets}


check("done today and the two before", streak(days(0, 1, 2), TODAY), 3)
check("not done yet today: yesterday's streak still counts", streak(days(1, 2, 3), TODAY), 3)
check("missed yesterday: no streak", streak(days(2, 3, 4), TODAY), 0)
check("nothing", streak(set(), TODAY), 0)
check("best run", best(days(0, 1, 5, 6, 7, 8)), 4)


async def integration() -> None:
    from sloane.memory.store import Store

    config = isolated(database_url=os.environ["DATABASE_URL"], timezone="America/Denver")
    now = {"at": datetime(2026, 9, 24, 21, 0, tzinfo=DEN)}
    async with Store(config) as store:
        await store._exec("truncate habits, habit_log cascade")
        ctx = SkillContext(store=store, config=config, clock=lambda: now["at"])
        habits = Habits(ctx)
        reg = Registry([habits], ctx)

        async def cmd(name, rest=""):
            return (await reg.command(name, rest)).speech

        async def say(text):
            answer = await reg.route(text)
            return None if answer is None else answer.speech

        check("nothing tracked", await cmd("habits"), "You aren't tracking any habits yet.")
        check("no habits: 'did x' is the agent's", await say("did reading"), None)
        check("add", await cmd("habit", "add reading"), 'Tracking reading. Say "did reading" when it\'s done.')
        check("add again", await cmd("habit", "add Reading"), "You already track Reading.")
        await cmd("habit", "add gym")
        await cmd("habit", "add morning stretches")

        # Build a three-day reading streak going into today.
        check("backfill yesterday", await cmd("did", "reading yesterday"), "Marked reading for yesterday. Streak: 1 day.")
        check("a date too old", await cmd("did", "reading sept 1"), "I can only mark habits for the last week.")
        await cmd("did", "reading tuesday")
        facts = await habits.facts()
        check("FACTS", facts, [
            "- HABIT reading: 2 days streak, not done yet today",
            "- HABIT gym: 0 days streak, not done yet today",
            "- HABIT morning stretches: 0 days streak, not done yet today",
        ])
        nudges = await habits.nudges()
        check("evening nudge for a streak about to break", [n.text for n in nudges],
              ["🔥 Reading isn't done today; your 2-day streak ends at midnight."])
        check("keyed by day", nudges[0].key, "habits:2026-09-24")

        check("plain 'did reading'", await say("Did reading!"), "Marked reading. Streak: 3 days. New best.")
        check("again", await say("done with reading"), "Reading was already done today. Streak: 3 days.")
        check("prefix of a longer name", await say("finished my morning stretches today"),
              "Marked morning stretches. Streak: 1 day.")
        check("'did you ...' is the agent's", await say("did you get my email?"), None)
        check("a word that isn't a habit", await say("did laundry"), None)
        check("no nudge once done", await habits.nudges(), [])
        check("listing", (await reg.command("habits", "")).speech, "1 left today: gym.")
        detail = (await reg.command("habits", "")).detail
        check("listing detail", detail.splitlines()[0], "✓ reading: 3 days (best 3, 3/7 this week)")
        check("undo", await cmd("habit", "undo reading"), "Unmarked reading. Streak: 2 days.")
        check("undo twice", await cmd("habit", "undo reading"), "Reading wasn't marked today.")
        check("drop", await cmd("habit", "drop gym"), "Stopped tracking gym.")
        check("gone from the list", "gym" in (await reg.command("habits", "")).detail, False)
        now["at"] = datetime(2026, 9, 24, 12, 0, tzinfo=DEN)
        check("no nudge at noon", await habits.nudges(), [])
        check("panel", (await habits.panel())["lines"][0], "· reading — 2 days")


if os.environ.get("DATABASE_URL"):
    asyncio.run(integration())

if FAILURES:
    print(f"FAIL ({len(FAILURES)})")
    for f in FAILURES:
        print("  -", f)
    raise SystemExit(1)
print("habits: streaks, marking by rule and by command, undo, the evening nudge")
