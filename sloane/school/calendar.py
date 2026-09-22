"""The secret .ics feed.

A calendar URL is a bearer credential in a query string: anyone holding it can
read the whole calendar. It is treated as a secret, never logged, and never
echoed into a reply.

Two things this handles that a naive VEVENT loop does not:

* Recurrence. A weekly event is one VEVENT with an RRULE, not fifty rows. It is
  expanded to one row per occurrence in the window, keyed uid:occurrence, so a
  re-sync updates rather than duplicates. Without this, "what's on Thursday"
  silently misses every repeating thing on the calendar.
* All-day events. Their DTSTART is a date, not a datetime, and attaching UTC to
  it shifts the event a day for anyone west of Greenwich -- which is everyone
  in Colorado. They are anchored in Landen's timezone instead.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from sloane.ingest import safe_field
from sloane.school import SchoolError

log = logging.getLogger(__name__)

# A school calendar is small; anything this size is a wrong URL.
MAX_BYTES = 8 * 1024 * 1024

# Guards a pathological RRULE (COUNT=100000) from filling the table.
MAX_OCCURRENCES = 500


async def fetch(url: str, *, timeout: float = 30.0) -> bytes:
    """Download the feed. The URL is a credential and never reaches a log."""
    if not url:
        raise SchoolError("CALENDAR_ICS_URL is not set")
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.get(url)
    except httpx.HTTPError as exc:
        # Deliberately not interpolating the URL: it is a secret.
        raise SchoolError(f"calendar feed unreachable: {type(exc).__name__}") from exc

    if response.status_code >= 400:
        raise SchoolError(f"calendar feed returned {response.status_code}")
    body = response.content
    if len(body) > MAX_BYTES:
        raise SchoolError(f"calendar feed is {len(body)} bytes; refusing to parse")
    if b"BEGIN:VCALENDAR" not in body[:2048]:
        raise SchoolError("that URL did not return an iCalendar feed")
    return body


def _as_aware(value: Any, tz: ZoneInfo) -> tuple[datetime | None, bool]:
    """Normalise a DTSTART/DTEND to an aware datetime, flagging all-day."""
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=tz)), False
    if isinstance(value, date):
        # A bare date is all-day *in Landen's timezone*, not in UTC.
        return datetime.combine(value, time.min, tzinfo=tz), True
    return None, False


def parse(
    body: bytes,
    *,
    window_start: datetime,
    window_end: datetime,
    tz: str = "UTC",
) -> list[dict]:
    """Every occurrence falling inside the window, expanded and sanitised."""
    try:
        from icalendar import Calendar
    except ImportError as exc:  # pragma: no cover - env-dependent
        raise SchoolError("the icalendar package is not installed") from exc

    try:
        cal = Calendar.from_ical(body)
    except Exception as exc:  # noqa: BLE001 - malformed feeds fail many ways
        raise SchoolError(f"could not parse the calendar feed: {exc}") from exc

    zone = ZoneInfo(tz)
    out: list[dict] = []

    for component in cal.walk("VEVENT"):
        title = safe_field(component.get("SUMMARY"), limit=200)
        if not title:
            continue
        uid = safe_field(component.get("UID"), limit=200) or title

        dtstart = component.get("DTSTART")
        start, all_day = _as_aware(getattr(dtstart, "dt", None), zone)
        if start is None:
            continue

        dtend = component.get("DTEND")
        end, _ = _as_aware(getattr(dtend, "dt", None), zone)
        duration = (end - start) if end else (
            timedelta(days=1) if all_day else timedelta(hours=1)
        )

        location = safe_field(component.get("LOCATION"), limit=200) or None
        starts = _occurrences(component, start, window_start, window_end)

        for occurrence in starts:
            out.append(
                {
                    "external_id": f"{uid}:{occurrence.isoformat()}",
                    "title": title,
                    "starts_at": occurrence,
                    "ends_at": occurrence + duration,
                    "all_day": all_day,
                    "location": location,
                }
            )

    out.sort(key=lambda e: e["starts_at"])
    return out


def _occurrences(
    component: Any,
    start: datetime,
    window_start: datetime,
    window_end: datetime,
) -> list[datetime]:
    """Expand an RRULE, or return the single start if there is none."""
    rrule_prop = component.get("RRULE")
    if rrule_prop is None:
        return [start] if window_start <= start <= window_end else []

    try:
        from dateutil.rrule import rrulestr
    except ImportError:  # pragma: no cover - ships with icalendar
        log.warning("dateutil missing; recurring events will not be expanded")
        return [start] if window_start <= start <= window_end else []

    try:
        rule = rrulestr(
            rrule_prop.to_ical().decode(), dtstart=start, forceset=True
        )
        # EXDATE: occurrences the organiser removed.
        exdates = component.get("EXDATE")
        for prop in exdates if isinstance(exdates, list) else ([exdates] if exdates else []):
            for excluded in getattr(prop, "dts", []):
                moment = getattr(excluded, "dt", None)
                if isinstance(moment, datetime):
                    rule.exdate(moment if moment.tzinfo else moment.replace(tzinfo=start.tzinfo))
    except Exception as exc:  # noqa: BLE001 - a bad rule must not lose the feed
        log.warning("unparseable RRULE, keeping the single occurrence: %s", exc)
        return [start] if window_start <= start <= window_end else []

    found = []
    for moment in rule.between(window_start, window_end, inc=True):
        found.append(moment)
        if len(found) >= MAX_OCCURRENCES:
            log.warning("RRULE produced more than %s occurrences; truncating", MAX_OCCURRENCES)
            break
    return found
