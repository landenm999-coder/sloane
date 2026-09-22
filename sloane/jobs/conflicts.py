"""Finding collisions in the day, in code.

The P2 gate is five school days of briefs he actually used, with zero missed
conflicts. "Zero missed" cannot rest on the model spotting an overlap in a list
of times -- it will, usually, and usually is not the bar. So the overlaps are
computed here, deterministically, and handed over as findings.

What counts as a conflict for Landen specifically:

* Work due *while he is on shift*. Due at 4 PM on a weekday is a real problem;
  due at 11:59 PM the same day is not, because he is home by 7 with hours to
  spare. The naive version -- "anything due on a shift day" -- cries wolf five
  times a week and gets ignored, which is worse than silence.
* Anything else scheduled inside the shift. A 5 PM DECA meeting is a conflict
  whether or not he realised when he accepted it.
* Two events on top of each other.
* Work due in the hour after a shift ends. Not impossible, but he walks in at
  7:05 and it is due at 7:30 -- he should hear that this morning, not at 7:06.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

Row = dict[str, Any]

# How long after a shift ends a due time still counts as uncomfortably close.
TIGHT_AFTER_SHIFT = timedelta(minutes=60)

HARD = "hard"
TIGHT = "tight"


@dataclass(frozen=True)
class Conflict:
    kind: str
    severity: str
    what: str
    when: datetime
    against: str

    def line(self) -> str:
        return f"{self.what} vs {self.against}"


def _overlaps(
    a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime
) -> bool:
    """Half-open intervals: touching end-to-start is not an overlap.

    A shift ending at 7 PM and a meeting starting at 7 PM is a tight evening,
    not a collision, and flagging it as one is how an alert stops being read.
    """
    return a_start < b_end and b_start < a_end


def _within(moment: datetime, start: datetime, end: datetime) -> bool:
    return start <= moment < end


def find(
    *,
    assignments: Sequence[Row] = (),
    shifts: Sequence[Row] = (),
    events: Sequence[Row] = (),
    tight_after_shift: timedelta = TIGHT_AFTER_SHIFT,
) -> list[Conflict]:
    """Every collision, most severe first, then chronological."""
    found: list[Conflict] = []

    live_shifts = [s for s in shifts if not s.get("cancelled")]

    for shift in live_shifts:
        s_start, s_end = shift.get("starts_at"), shift.get("ends_at")
        if not isinstance(s_start, datetime) or not isinstance(s_end, datetime):
            continue
        shift_label = "your shift"

        # Work due while he is at work.
        for a in assignments:
            due = a.get("due_at")
            if not isinstance(due, datetime) or a.get("status") not in (None, "open", "missing"):
                continue
            if _within(due, s_start, s_end):
                found.append(
                    Conflict(
                        kind="due_during_shift", severity=HARD,
                        what=str(a.get("title", "something")), when=due,
                        against=shift_label,
                    )
                )
            elif s_end <= due < s_end + tight_after_shift:
                found.append(
                    Conflict(
                        kind="due_just_after_shift", severity=TIGHT,
                        what=str(a.get("title", "something")), when=due,
                        against=f"{shift_label} ending",
                    )
                )

        # Anything else booked inside the shift.
        for e in events:
            e_start = e.get("starts_at")
            e_end = e.get("ends_at") or e_start
            if not isinstance(e_start, datetime) or not isinstance(e_end, datetime):
                continue
            if e.get("all_day"):
                continue  # an all-day marker is not a collision
            # A point-in-time event has no span, and half-open overlap on a
            # zero-length interval collapses to `start < start` -- so a call at
            # exactly 3:00 PM, the minute the shift starts, would slip through.
            # Treat it like a due time instead: inside if it lands in the shift.
            clashes = (
                _within(e_start, s_start, s_end)
                if e_end <= e_start
                else _overlaps(e_start, e_end, s_start, s_end)
            )
            if clashes:
                found.append(
                    Conflict(
                        kind="event_during_shift", severity=HARD,
                        what=str(e.get("title", "an event")), when=e_start,
                        against=shift_label,
                    )
                )

    # Two things booked at once.
    timed = [
        e for e in events
        if isinstance(e.get("starts_at"), datetime) and not e.get("all_day")
    ]
    for i, first in enumerate(timed):
        for second in timed[i + 1:]:
            f_start = first["starts_at"]
            f_end = first.get("ends_at") or f_start
            s_start = second["starts_at"]
            s_end = second.get("ends_at") or s_start
            if _overlaps(f_start, f_end, s_start, s_end):
                found.append(
                    Conflict(
                        kind="event_overlaps_event", severity=HARD,
                        what=str(first.get("title", "an event")), when=f_start,
                        against=str(second.get("title", "another event")),
                    )
                )

    found.sort(key=lambda c: (c.severity != HARD, c.when))
    return found


def render(conflicts: Sequence[Conflict], tz: str = "UTC") -> list[str]:
    """FACTS lines. Phrased as findings, because that is what they are."""
    from sloane.memory.tiers import _when

    lines = []
    for c in conflicts:
        mark = "CONFLICT" if c.severity == HARD else "TIGHT"
        lines.append(f"- {mark}: {c.what} at {_when(c.when, tz)} clashes with {c.against}")
    return lines
