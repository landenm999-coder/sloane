"""Canvas without a token: assignments from its Calendar Feed.

Some districts don't let students make access tokens. Every student still has
Canvas → Calendar → Calendar Feed: an .ics link carrying every assignment's due
date. It is a thinner source than the API -- no turned-in state, no grades --
so what it can't know, it doesn't guess:

* An assignment still ahead is 'open'. One whose due time has passed is
  'unknown', not overdue: the feed can't say whether it went in, and nagging
  him about work he turned in is worse than saying nothing. Telling her he
  finished something still marks it done.
* Rows use the API's own keys (source 'canvas', the assignment's id, the
  course's id), so a token added later updates these rows instead of doubling
  them.

The link is a credential, like the calendar's: it never reaches a log.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from sloane.ingest import safe_field
from sloane.school import SchoolError

_ASSIGNMENT_UID = re.compile(r"^event-assignment-(override-)?(\d+)$")
_ASSIGNMENT_IN_URL = re.compile(r"(?:/assignments/|#assignment_)(\d+)")
_COURSE_IN_URL = re.compile(r"(?:/courses/|course_)(\d+)")
_COURSE_SUFFIX = re.compile(r"^(.*\S)\s*\[([^\[\]]+)\]\s*$")
# Canvas shows an assignment due at 11:59 PM as all-day; that is its due time.
_END_OF_DAY = time(23, 59)


def _due(value: Any, zone: ZoneInfo) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=zone)
    if isinstance(value, date):
        return datetime.combine(value, _END_OF_DAY, tzinfo=zone)
    return None


def parse_feed(body: bytes, *, window_start: datetime, window_end: datetime, tz: str = "UTC") -> list[dict]:
    """The assignments in a Canvas Calendar Feed, due inside the window.

    Course calendar events (a pep rally, a field trip) are left out: they belong
    to the calendar, and his Google calendar is where he keeps those.
    """
    try:
        from icalendar import Calendar

        cal = Calendar.from_ical(body)
    except Exception as exc:  # noqa: BLE001 - malformed feeds fail many ways
        raise SchoolError(f"could not parse the Canvas feed: {exc}") from exc

    zone = ZoneInfo(tz)
    out: dict[str, dict] = {}
    for component in cal.walk("VEVENT"):
        uid = str(component.get("UID") or "").strip()
        is_assignment = _ASSIGNMENT_UID.match(uid)
        if not is_assignment:
            continue
        url = safe_field(component.get("URL"), limit=400) or ""
        from_url = _ASSIGNMENT_IN_URL.search(url)
        if from_url:
            external_id = from_url.group(1)
        elif is_assignment.group(1):  # a section's own date, and no link to its assignment
            external_id = f"override-{is_assignment.group(2)}"
        else:
            external_id = is_assignment.group(2)

        summary = safe_field(component.get("SUMMARY"), limit=300)
        suffix = _COURSE_SUFFIX.match(summary)
        title, course_name = (suffix.group(1), suffix.group(2).strip()) if suffix else (summary, "")
        if not title:
            continue
        course = _COURSE_IN_URL.search(url)
        course_external_id = course.group(1) if course else (f"feed:{course_name}" if course_name else None)

        due = _due(getattr(component.get("DTSTART"), "dt", None), zone)
        if due is None or not (window_start <= due <= window_end):
            continue
        out[external_id] = {
            "external_id": external_id,
            "title": title,
            "due_at": due,
            "course_external_id": course_external_id,
            "course_name": course_name or None,
            "url": url if url.startswith("https://") and "/assignments/" in url else None,
        }
    # An item with only its course's name joins that course's id where another
    # item names it, or the same class ends up as two course rows.
    ids = {a["course_name"]: a["course_external_id"] for a in out.values()
           if a["course_name"] and not str(a["course_external_id"]).startswith("feed:")}
    for a in out.values():
        if str(a["course_external_id"]).startswith("feed:") and a["course_name"] in ids:
            a["course_external_id"] = ids[a["course_name"]]
    return sorted(out.values(), key=lambda a: a["due_at"])
