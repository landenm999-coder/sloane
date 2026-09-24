"""Focus sessions: the timer is a reminder, stopping cancels it, the day adds up.

DESTRUCTIVE: truncates focus_sessions and reminders.
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
from sloane.skills.focus import Focus

FAILURES: list[str] = []
DEN = ZoneInfo("America/Denver")


def check(label: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{label}\n     got: {got!r}\n    want: {want!r}")


async def integration() -> None:
    from sloane.memory.store import Store

    config = isolated(database_url=os.environ["DATABASE_URL"], timezone="America/Denver")
    now = {"at": datetime(2026, 9, 24, 16, 0, tzinfo=DEN)}
    async with Store(config) as store:
        await store._exec("truncate focus_sessions, reminders cascade")
        ctx = SkillContext(store=store, config=config, clock=lambda: now["at"])
        focus = Focus(ctx)
        reg = Registry([focus], ctx)

        async def cmd(rest=""):
            return (await reg.command("focus", rest)).speech

        check("nothing yet", await cmd(), "No focus time yet today.")
        check("start", await cmd("25 physics lab"), "Focusing on physics lab for 25 min; I'll tell you at 4:25 PM.")
        reminders = await store._fetch("select text, due_at, source from reminders")
        check("the end is a real reminder", [(r["text"], r["due_at"], r["source"]) for r in reminders],
              [("⏱ Time's up: physics lab (25 min). Take five.", datetime(2026, 9, 24, 16, 25, tzinfo=DEN), "focus")])
        now["at"] += timedelta(minutes=10)
        check("status while running", await cmd(), "15 min left on physics lab. 10 min today so far.")
        check("FACTS while running", await focus.facts(),
              ["- FOCUS today: 10 min (physics lab 10); running now: physics lab until 4:25 PM"])
        check("stop early", await cmd("stop"), "Stopped physics lab after 10 min. 10 min today.")
        cancelled = await store._one("select cancelled_at from reminders")
        check("stopping cancels the reminder", cancelled["cancelled_at"] is not None, True)
        check("stop with nothing running", await cmd("stop"), "No focus session is running.")

        check("no number: 25 minutes", await cmd("essay"), "Focusing on essay for 25 min; I'll tell you at 4:35 PM.")
        now["at"] += timedelta(minutes=5)
        check("a new one ends the old", await cmd("45 on reading"),
              "Focusing on reading for 45 min; I'll tell you at 5:00 PM. (ended essay first)")
        now["at"] += timedelta(hours=2)  # reading ran its full 45
        check("the day adds up", await cmd(), "1h of focus today.")
        detail = (await reg.command("focus", "")).detail
        check("by what", detail.splitlines()[:3], ["• reading: 45 min", "• physics lab: 10 min", "• essay: 5 min"])
        check("too long", await cmd("500 everything"), "Pick between 1 and 240 minutes.")
        check("a task starting with m keeps its m", await cmd("25 math homework"),
              "Focusing on math homework for 25 min; I'll tell you at 6:40 PM.")
        check("minutes spelled out", await cmd("30 minutes essay"),
              "Focusing on essay for 30 min; I'll tell you at 6:45 PM. (ended math homework first)")
        check("min", await cmd("20 min essay"),
              "Focusing on essay for 20 min; I'll tell you at 6:35 PM. (ended essay first)")
        await cmd("stop")
        check("panel", (await focus.panel())["lines"], ["1h today"])
        live = await store._fetch("select count(*) as n from reminders where cancelled_at is null")
        check("only the finished one's reminder is left", live[0]["n"], 1)


if os.environ.get("DATABASE_URL"):
    asyncio.run(integration())

if FAILURES:
    print(f"FAIL ({len(FAILURES)})")
    for f in FAILURES:
        print("  -", f)
    raise SystemExit(1)
print("focus: timer via a reminder, stop cancels it, one at a time, the day's total")
