"""Should-have-caught-this. No database, no model.

The P2 gate is zero missed conflicts across five school days. That makes every
case below a promise: the ones that must fire, and -- just as much -- the ones
that must not. A detector that flags "due at 11:59 PM on a work day" five times
a week gets ignored by Thursday, and an ignored alert catches nothing.

All times are Denver wall-clock, via the same helper the briefs use.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sloane.jobs.conflicts import HARD, TIGHT, find, render

FAILURES: list[str] = []
DEN = ZoneInfo("America/Denver")


def check(label: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{label}\n     got: {got!r}\n    want: {want!r}")


def at(hour: int, minute: int = 0, day: int = 25) -> datetime:
    """A wall-clock time in Parker. Fri Sep 25 2026 is a school day."""
    return datetime(2026, 9, day, hour, minute, tzinfo=DEN)


SHIFT = {"starts_at": at(15), "ends_at": at(19)}


def due(title: str, when: datetime, status: str = "open") -> dict:
    return {"title": title, "due_at": when, "status": status}


def event(title: str, start: datetime, end: datetime | None = None, all_day=False) -> dict:
    return {"title": title, "starts_at": start, "ends_at": end, "all_day": all_day}


def kinds(conflicts) -> list[str]:
    return [c.kind for c in conflicts]


# === MUST CATCH ============================================================

check(
    "work due in the middle of a shift",
    kinds(find(assignments=[due("Stat quiz", at(16))], shifts=[SHIFT])),
    ["due_during_shift"],
)
check(
    "work due the minute the shift starts",
    kinds(find(assignments=[due("Lab", at(15))], shifts=[SHIFT])),
    ["due_during_shift"],
)
check(
    "a meeting booked inside the shift",
    kinds(find(shifts=[SHIFT], events=[event("DECA", at(17), at(18))])),
    ["event_during_shift"],
)
check(
    "a meeting that starts before and runs into the shift",
    kinds(find(shifts=[SHIFT], events=[event("Tutoring", at(14), at(16))])),
    ["event_during_shift"],
)
check(
    "a point-in-time event with no end, inside the shift",
    kinds(find(shifts=[SHIFT], events=[event("Call", at(16))])),
    ["event_during_shift"],
)
check(
    "a point-in-time event at the exact minute the shift starts",
    kinds(find(shifts=[SHIFT], events=[event("Call", at(15))])),
    ["event_during_shift"],
)
check(
    "a point-in-time event at the exact minute the shift ends is fine",
    find(shifts=[SHIFT], events=[event("Call", at(19))]), [],
)
check(
    "two things booked on top of each other",
    kinds(find(events=[event("DECA", at(18), at(19)), event("Study group", at(18, 30), at(20))])),
    ["event_overlaps_event"],
)
check(
    "missing work is still live work",
    kinds(find(assignments=[due("Essay", at(17), status="missing")], shifts=[SHIFT])),
    ["due_during_shift"],
)
check(
    "due half an hour after getting off",
    [(c.kind, c.severity) for c in find(assignments=[due("Reading", at(19, 30))], shifts=[SHIFT])],
    [("due_just_after_shift", TIGHT)],
)

# === MUST NOT CRY WOLF =====================================================

check(
    "due at 11:59 PM on a work day is fine -- he is home by 7",
    find(assignments=[due("Stat p. 214", at(23, 59))], shifts=[SHIFT]), [],
)
check(
    "due two hours after the shift is not tight",
    find(assignments=[due("Reading", at(21))], shifts=[SHIFT]), [],
)
check(
    "a meeting starting exactly when the shift ends is a busy evening, not a clash",
    find(shifts=[SHIFT], events=[event("DECA", at(19), at(20))]), [],
)
check(
    "a meeting ending exactly when the shift starts is not a clash either",
    find(shifts=[SHIFT], events=[event("Lunch", at(14), at(15))]), [],
)
check(
    "back-to-back events are not overlapping",
    find(events=[event("A", at(10), at(11)), event("B", at(11), at(12))]), [],
)
check(
    "an all-day marker never clashes -- 'No school' is not a meeting",
    find(shifts=[SHIFT], events=[event("No school", at(0), at(0, day=26), all_day=True)]), [],
)
check(
    "submitted work is done, whatever its due time",
    find(assignments=[due("Quiz", at(16), status="submitted")], shifts=[SHIFT]), [],
)
check(
    "graded work is done",
    find(assignments=[due("Quiz", at(16), status="graded")], shifts=[SHIFT]), [],
)
check(
    "a cancelled shift conflicts with nothing",
    find(assignments=[due("Quiz", at(16))], shifts=[{**SHIFT, "cancelled": True}]), [],
)
check(
    "work due the morning before a shift is not a shift conflict",
    find(assignments=[due("Homework", at(8))], shifts=[SHIFT]), [],
)
check("a day with nothing in it has nothing to report", find(), [])

# === ORDER AND WORDING =====================================================

mixed = find(
    assignments=[due("Late reading", at(19, 20)), due("Quiz", at(16))],
    shifts=[SHIFT],
    events=[event("DECA", at(17), at(18))],
)
check(
    "hard conflicts lead, then tight ones, each in time order",
    [(c.severity, c.what) for c in mixed],
    [(HARD, "Quiz"), (HARD, "DECA"), (TIGHT, "Late reading")],
)

lines = render(mixed, tz="America/Denver")
check("rendered as one FACTS line each", len(lines), 3)
check("hard ones say CONFLICT", lines[0].startswith("- CONFLICT:"), True)
check("tight ones say TIGHT", lines[2].startswith("- TIGHT:"), True)
check("times are local", "4 PM" in lines[0], True)

# Garbage in must not become a crash in a 6:35 AM job.
check(
    "rows missing their times are skipped, not fatal",
    find(assignments=[{"title": "x"}], shifts=[{"starts_at": None}], events=[{"title": "y"}]),
    [],
)

# Across a whole week of the real rule, nothing should fire on its own.
from sloane.school.shifts import planned_shifts

week = [
    {"starts_at": s, "ends_at": e}
    for s, e in planned_shifts(at(0).date(), 1, tz="America/Denver", start_hour=15, end_hour=19)
]
nightly = [due(f"Homework {i}", at(23, 59, day=21 + i)) for i in range(5)]
check(
    "a normal week -- shifts every weekday, homework due every night -- is quiet",
    find(assignments=nightly, shifts=week), [],
)

# -- two calls booked for the same instant are a collision -----------------
check(
    "two point-in-time events at the same minute collide",
    kinds(find(events=[event("Call A", at(20)), event("Call B", at(20))])),
    ["event_overlaps_event"],
)
check(
    "a point event inside a timed one collides",
    kinds(find(events=[event("Call", at(20, 30)), event("Dinner", at(20), at(21))])),
    ["event_overlaps_event"],
)
check(
    "a point event at another's end does not",
    find(events=[event("Call", at(21)), event("Dinner", at(20), at(21))]), [],
)

if FAILURES:
    print(f"FAIL ({len(FAILURES)})")
    for f in FAILURES:
        print("  -", f)
    raise SystemExit(1)
print("conflicts: every must-catch caught, every false alarm refused")
