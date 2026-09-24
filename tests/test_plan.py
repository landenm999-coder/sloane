"""The study plan: free time around the shift and calendar, filled by due date.

DESTRUCTIVE: truncates assignments, shifts, events and courses.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _settings import isolated

from sloane.skills import Registry, SkillContext
from sloane.skills.plan import Block, Planner, fill, free_blocks, guess_minutes, parse_duration, span

FAILURES: list[str] = []
DEN = ZoneInfo("America/Denver")
THU = date(2026, 9, 24)


def check(label: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{label}\n     got: {got!r}\n    want: {want!r}")


def at(hour: int, minute: int = 0, day: date = THU) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=DEN)


check("durations", [parse_duration(x) for x in ["2h", "90m", "1.5 hours", "1h 30m", "45", "soon", "0"]],
      [120, 90, 90, 90, 45, None, None])
check("guesses from the title", [guess_minutes(t) for t in ["Unit 3 Test", "Final essay draft", "Lab 4 writeup",
                                                              "Read chapter 5", "Worksheet 2.3", "Something"]],
      [60, 90, 60, 40, 30, 30])
check("span", [span(45), span(60), span(135)], ["45m", "1h", "2h 15m"])

# Free time: 3 PM to 10:30 PM, minus a 3-7 shift with a 30-minute commute each side.
blocks = free_blocks(THU, now=at(9), busy=[(at(14, 30), at(19, 30))], start=time(15), end=time(22, 30), zone=DEN)
check("the shift and commute are carved out", [(b.start, b.end) for b in blocks], [(at(19, 30), at(22, 30))])
blocks = free_blocks(THU, now=at(20, 3), busy=[(at(14, 30), at(19, 30))], start=time(15), end=time(22, 30), zone=DEN)
check("nothing before now, rounded to five minutes", blocks[0].start, at(20, 5))
blocks = free_blocks(THU, now=at(9), busy=[(at(19, 30), at(19, 40)), (at(20, 0), at(21, 0))],
                     start=time(19, 30), end=time(22, 30), zone=DEN)
check("slivers under 15 minutes are dropped", [(b.start, b.end) for b in blocks],
      [(at(19, 40), at(20, 0)), (at(21, 0), at(22, 30))])

slots, unplaced = fill([Block(at(19, 30), at(21, 0))], [("A", "due tomorrow", 60), ("B", "due Fri", 60)])
check("in order, with a break between", [(s.title, s.start, s.end) for s in slots],
      [("A", at(19, 30), at(20, 30)), ("B", at(20, 40), at(21, 0))])
check("what doesn't fit is said", unplaced, [("B", "due Fri", 40)])
slots, _ = fill([Block(at(16), at(16, 45)), Block(at(19, 30), at(21))], [("Essay", "due Fri", 90)])
check("a long task splits into parts", [(s.part, s.start, s.end) for s in slots],
      [("(part 1)", at(16), at(16, 45)), ("(part 2)", at(19, 30), at(20, 15))])


async def integration() -> None:
    from sloane.memory.store import Store

    config = isolated(database_url=os.environ["DATABASE_URL"], timezone="America/Denver")
    now = {"at": at(13)}
    async with Store(config) as store:
        await store._exec("truncate assignments, shifts, events, courses cascade")
        course = await store.create_course(name="Physics", source="canvas", external_id="c1")
        await store.add_shift(at(15), at(19))
        await store.upsert_event(title="DECA meeting", starts_at=at(20), ends_at=at(20, 30), external_id="e1")

        async def assignment(ext, title, due, **kw):
            await store.upsert_assignment(title=title, source="canvas", external_id=ext, due_at=due,
                                          course_id=course, **kw)

        await assignment("a1", "Lab 4 writeup", at(8, 0, THU + timedelta(days=1)))       # 60m guess
        await assignment("a2", "Chapter 3 reading", at(23, 59, THU + timedelta(days=2)))  # 40m guess
        await assignment("a3", "Unit test study guide", at(23, 59, THU + timedelta(days=3)))
        await assignment("a4", "Old worksheet", at(8, 0, THU - timedelta(days=2)), status="missing")
        await assignment("a5", "Far away project", at(8, 0, THU + timedelta(days=10)))

        ctx = SkillContext(store=store, config=config, clock=lambda: now["at"])
        planner = Planner(ctx)
        reg = Registry([planner], ctx)

        plan = await reg.command("plan", "")
        check("speech", plan.speech,
              "2h 30m free tonight and 3h 10m of work. Start with Old worksheet at 7:30 PM; 1 won't fit.")
        check("the plan", plan.detail.splitlines(), [
            "7:30 PM–8:00 PM  Old worksheet [Physics] — overdue",
            "8:30 PM–9:30 PM  Lab 4 writeup [Physics] — due tomorrow 8:00 AM",
            "9:40 PM–10:20 PM  Chapter 3 reading [Physics] — due Sat 11:59 PM",
            "",
            "Won't fit:",
            "• Unit test study guide [Physics] — due Sun 11:59 PM (about 1h left)",
        ])
        check("the DECA meeting is carved out", "8:00 PM–8:30 PM" in plan.detail, False)

        check("an estimate", (await reg.command("estimate", "lab 4 writeup 1h 30m")).speech,
              "Got it: Lab 4 writeup takes about 1h 30m.")
        check("an ambiguous estimate", (await reg.command("estimate", "physics 20m")).speech, "5 match; say a bit more.")
        check("a bad duration", (await reg.command("estimate", "lab 4 soon")).speech, "How long? Try 45m, 2h or 1h 30m.")
        facts = await planner.facts()
        check("FACTS", facts, ["- PLAN tonight: 2h 30m free from 7:30 PM; 3h 40m of work due in the next 4 days; "
                               "first: Old worksheet [Physics]; 2 won't fit"])
        check("the estimate survives a re-sync", (await store.upsert_assignment(
            title="Lab 4 writeup", source="canvas", external_id="a1", due_at=at(8, 0, THU + timedelta(days=1)),
            course_id=course)) is not None and (await store._one(
                "select estimate_minutes from assignments where external_id = 'a1'"))["estimate_minutes"], 90)

        tomorrow = await reg.route("what's my plan for tomorrow?")
        check("tomorrow: today's work is not tomorrow's", "Lab 4 writeup" in tomorrow.detail, False)
        check("tomorrow has the afternoon (no shift generated)", tomorrow.detail.splitlines()[0].startswith("3:00 PM"), True)
        check("plain words", (await reg.route("plan my night")).speech.startswith("2h 30m free tonight"), True)
        check("not ours", await reg.route("what's the plan for the essay?"), None)

        now["at"] = at(22, 40)
        check("past bedtime", (await reg.command("plan", "")).speech, "You don't have free time left tonight.")
        await store._exec("update assignments set done_locally = true")
        check("nothing due", (await reg.command("plan", "")).speech, "Nothing due in the next 4 days, so tonight is yours.")
        check("no FACTS line with nothing due", await planner.facts(), [])


if os.environ.get("DATABASE_URL"):
    asyncio.run(integration())

if FAILURES:
    print(f"FAIL ({len(FAILURES)})")
    for f in FAILURES:
        print("  -", f)
    raise SystemExit(1)
print("plan: free time around shifts and events, filled by due date, estimates, FACTS")
