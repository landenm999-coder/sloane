"""/today and /week: rendered by rules from rows. No model, no database."""

from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sloane import views

FAILURES: list[str] = []
DEN = ZoneInfo("America/Denver")
TZ = "America/Denver"
TUE = date(2026, 9, 22)


def check(label: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{label}\n     got: {got!r}\n    want: {want!r}")


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=DEN)


SHIFTS = [{"starts_at": at(d, 15), "ends_at": at(d, 19), "cancelled": False} for d in (21, 22, 23, 24, 25)]
ASSIGNMENTS = [
    {"title": "Stat quiz", "due_at": at(22, 16), "course": "Statistical Reasoning"},
    {"title": "Lab report", "due_at": at(22, 23, 59), "course": "Chemistry"},
    {"title": "Essay", "due_at": at(24, 23, 59), "course": "Colorado History"},
    {"title": "Reading log", "due_at": at(24, 8), "course": "English"},
]
EVENTS = [
    {"title": "DECA call", "starts_at": at(22, 17), "ends_at": at(22, 18), "location": None},
    {"title": "Fall break", "starts_at": at(21, 0), "ends_at": at(24, 0), "all_day": True},
]

speech, detail = views.today(TUE, assignments=ASSIGNMENTS, shifts=SHIFTS, events=EVENTS,
                             overdue=[{"title": "Old log"}], tz=TZ)
check("today's spoken summary",
      speech, "Today: you work 3:00 PM to 7:00 PM, 2 things due, 2 conflicts, 1 overdue.")
lines = detail.splitlines()
check("header names the day", lines[0], "Tuesday September 22")
check("in time order, all-day first",
      lines[1:6],
      ["• Fall break (all day)", "• WORK 3:00 PM–7:00 PM", "• DUE 4:00 PM: Stat quiz [Statistical Reasoning]",
       "• 5:00 PM–6:00 PM DECA call", "• DUE 11:59 PM: Lab report [Chemistry]"])
check("conflicts are computed, not guessed",
      [l for l in lines if l.startswith("⚠")],
      ["⚠ CONFLICT: Stat quiz vs your shift", "⚠ CONFLICT: DECA call vs your shift"])
check("overdue is counted", lines[-1], "Overdue: 1 — most recent: Old log")

clear, clear_detail = views.today(date(2026, 9, 26), assignments=ASSIGNMENTS, shifts=SHIFTS,
                                  events=[], overdue=[], tz=TZ)
check("a clear Saturday says so", clear, "Today: no shift, nothing due.")
check("and shows nothing scheduled", clear_detail.splitlines()[1], "• Nothing scheduled.")

wspeech, wdetail = views.week(TUE, assignments=ASSIGNMENTS, shifts=SHIFTS, events=EVENTS, tz=TZ)
check("week summary names the busiest day", wspeech, "4 things due this week, most on Tuesday (2).")
blocks = wdetail.split("\n\n")
check("seven day blocks", len(blocks), 7)
check("labels", [b.splitlines()[0] for b in blocks[:3]], ["Today", "Tomorrow", "Thu Sep 24"])
check("fall break shows on Wednesday too (multi-day)", "Fall break (all day)" in blocks[1], True)
check("but not on Thursday, when it has ended", "Fall break" in blocks[2], False)
check("an empty day is a dash", blocks[5], "Sun Sep 27\n• —")
check("nothing due", views.week(TUE, assignments=[], shifts=[], events=[], tz=TZ)[0],
      "Nothing due in the next week.")

# -- long replies are split for Telegram, never cut ------------------------------
from sloane.telegram import split_message

long = "\n".join(f"• line {i} " + "x" * 90 for i in range(120))
parts = split_message(long)
check("every part fits", all(len(p) <= 4096 for p in parts), True)
check("nothing is lost", "\n".join(parts), long)
check("splits fall on line breaks", all(p.startswith("• line") for p in parts), True)
check("short text is one part", split_message("hi"), ["hi"])
check("one unbroken run still splits", [len(p) for p in split_message("y" * 9000)], [4096, 4096, 808])

if FAILURES:
    print(f"FAIL ({len(FAILURES)})")
    for f in FAILURES:
        print("  -", f)
    raise SystemExit(1)
print("views: /today and /week render from rows, conflicts included")
