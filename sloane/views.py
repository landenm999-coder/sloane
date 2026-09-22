"""/today and /week: the day and the week straight from SQL, with no model involved.

These are the answers that must still arrive when every provider is down: what's
due, when he works, what's on the calendar, and what collides. They are rendered
by rules from the same rows the agent's FACTS block is built from, so the
two can never disagree about a date.

Pure functions over rows. The caller fetches the rows and builds the Reply.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sloane.jobs.conflicts import HARD, find

Row = dict[str, Any]


def _clock(moment: datetime, zone: ZoneInfo) -> str:
    local = moment.astimezone(zone)
    hour = local.hour % 12 or 12
    suffix = "AM" if local.hour < 12 else "PM"
    return f"{hour}:{local.minute:02d} {suffix}"


def _span(start: datetime, end: datetime | None, zone: ZoneInfo) -> str:
    if end is None or end <= start:
        return _clock(start, zone)
    return f"{_clock(start, zone)}–{_clock(end, zone)}"


def _local_day(value: Any, zone: ZoneInfo) -> date | None:
    return value.astimezone(zone).date() if isinstance(value, datetime) else None


def _covers(event: Row, day: date, zone: ZoneInfo) -> bool:
    """A multi-day event is on every day it spans (end exclusive)."""
    start = _local_day(event.get("starts_at"), zone)
    end_at = event.get("ends_at")
    if start is None:
        return False
    if not isinstance(end_at, datetime) or end_at <= event["starts_at"]:
        return start == day
    last = (end_at.astimezone(zone) - timedelta(microseconds=1)).date()
    return start <= day <= last


def day_lines(
    day: date,
    *,
    assignments: Sequence[Row],
    shifts: Sequence[Row],
    events: Sequence[Row],
    tz: str,
) -> list[str]:
    """One day as bullet lines, in time order. Empty list for a clear day."""
    zone = ZoneInfo(tz)
    items: list[tuple[datetime, str]] = []
    midnight = datetime.combine(day, datetime.min.time(), tzinfo=zone)

    day_shifts = [s for s in shifts if _local_day(s.get("starts_at"), zone) == day
                  and not s.get("cancelled")]
    day_events = [e for e in events if _covers(e, day, zone)]
    day_due = [a for a in assignments if _local_day(a.get("due_at"), zone) == day]

    for s in day_shifts:
        items.append((s["starts_at"], f"• WORK {_span(s['starts_at'], s.get('ends_at'), zone)}"))
    for e in day_events:
        where = f" @ {e['location']}" if e.get("location") else ""
        if e.get("all_day"):
            items.append((midnight, f"• {e['title']} (all day){where}"))
        else:
            items.append((e["starts_at"], f"• {_span(e['starts_at'], e.get('ends_at'), zone)} {e['title']}{where}"))
    for a in day_due:
        course = f" [{a['course']}]" if a.get("course") else ""
        when = "today" if a.get("all_day") else _clock(a["due_at"], zone)
        items.append((a["due_at"], f"• DUE {when}: {a['title']}{course}"))

    items.sort(key=lambda pair: pair[0])
    lines = [text for _, text in items]

    for c in find(assignments=day_due, shifts=day_shifts, events=[e for e in day_events if not e.get("all_day")]):
        mark = "⚠ CONFLICT" if c.severity == HARD else "⚠ tight"
        lines.append(f"{mark}: {c.what} vs {c.against}")
    return lines


def today(
    day: date,
    *,
    assignments: Sequence[Row],
    shifts: Sequence[Row],
    events: Sequence[Row],
    overdue: Sequence[Row],
    tz: str,
) -> tuple[str, str]:
    """(speech, detail) for one day."""
    lines = day_lines(day, assignments=assignments, shifts=shifts, events=events, tz=tz)
    zone = ZoneInfo(tz)
    due = [a for a in assignments if _local_day(a.get("due_at"), zone) == day]
    shift = next((s for s in shifts if _local_day(s.get("starts_at"), zone) == day
                  and not s.get("cancelled")), None)
    conflicts = sum(1 for line in lines if line.startswith("⚠ CONFLICT"))

    bits = []
    if shift:
        span = _span(shift["starts_at"], shift.get("ends_at"), zone).replace("–", " to ")
        bits.append(f"you work {span}")
    else:
        bits.append("no shift")
    bits.append(f"{len(due)} thing{'s' if len(due) != 1 else ''} due" if due else "nothing due")
    if conflicts:
        bits.append(f"{conflicts} conflict{'s' if conflicts != 1 else ''}")
    if overdue:
        bits.append(f"{len(overdue)} overdue")
    speech = "Today: " + ", ".join(bits) + "."

    header = f"{day:%A %B} {day.day}"
    body = lines or ["• Nothing scheduled."]
    if overdue:
        body.append(f"Overdue: {len(overdue)} — most recent: {overdue[0]['title']}")
    return speech, "\n".join([header, *body])


def week(
    start: date,
    *,
    assignments: Sequence[Row],
    shifts: Sequence[Row],
    events: Sequence[Row],
    tz: str,
    days: int = 7,
) -> tuple[str, str]:
    """(speech, detail) for the next `days` days, starting today."""
    blocks = []
    total_due = 0
    zone = ZoneInfo(tz)
    busiest: tuple[int, date] | None = None
    for offset in range(days):
        day = start + timedelta(days=offset)
        lines = day_lines(day, assignments=assignments, shifts=shifts, events=events, tz=tz)
        due_today = sum(1 for a in assignments if _local_day(a.get("due_at"), zone) == day)
        total_due += due_today
        if due_today and (busiest is None or due_today > busiest[0]):
            busiest = (due_today, day)
        label = "Today" if offset == 0 else ("Tomorrow" if offset == 1 else f"{day:%a %b} {day.day}")
        blocks.append(label + "\n" + ("\n".join(lines) if lines else "• —"))

    if total_due == 0:
        speech = "Nothing due in the next week."
    else:
        speech = f"{total_due} thing{'s' if total_due != 1 else ''} due this week"
        if busiest and busiest[0] > 1:
            speech += f", most on {busiest[1]:%A} ({busiest[0]})"
        speech += "."
    return speech, "\n\n".join(blocks)
