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
import re
from datetime import date, datetime, time, timedelta, timezone
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


async def fetch(url: str, *, timeout: float = 30.0, what: str = "calendar feed") -> bytes:
    """Download the feed. The URL is a credential and never reaches a log."""
    if not url:
        raise SchoolError(f"the {what} address is not set")
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.get(url)
    except httpx.HTTPError as exc:
        # Deliberately not interpolating the URL: it is a secret.
        raise SchoolError(f"{what} unreachable: {type(exc).__name__}") from exc

    if response.status_code >= 400:
        raise SchoolError(f"{what} returned {response.status_code}")
    body = response.content
    if len(body) > MAX_BYTES:
        raise SchoolError(f"{what} is {len(body)} bytes; refusing to parse")
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

    # A moved or cancelled single occurrence arrives as its own VEVENT with the
    # master's UID and a RECURRENCE-ID naming the slot it replaces. The master's
    # rule must skip that slot, or a class moved from 10:00 to noon shows twice.
    replaced: dict[str, list[datetime]] = {}
    for component in cal.walk("VEVENT"):
        rid = component.get("RECURRENCE-ID")
        if rid is not None:
            moment, _ = _as_aware(getattr(rid, "dt", None), zone)
            if moment is not None:
                uid = safe_field(component.get("UID"), limit=200)
                replaced.setdefault(uid, []).append(moment)

    for component in cal.walk("VEVENT"):
        title = safe_field(component.get("SUMMARY"), limit=200)
        if not title:
            continue
        if str(component.get("STATUS", "")).upper() == "CANCELLED":
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
        skip = replaced.get(uid, []) if component.get("RECURRENCE-ID") is None else []
        starts = _occurrences(component, start, window_start, window_end, zone=zone, skip=skip)

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


_UNTIL = re.compile(r"UNTIL=(\d{8})(T\d{6})?(Z?)", re.I)


def _utc_until(rule: str, zone: ZoneInfo) -> str:
    """Make UNTIL a UTC timestamp, which dateutil insists on for an aware start.

    Google writes all-day series as UNTIL=20261028 (a date) and some clients
    write a floating UNTIL=20261028T090000. With a timezone-aware DTSTART,
    dateutil rejects both, and the whole series collapses to its first
    occurrence. A date means "through the end of that day, in his timezone".
    """
    def fix(match: re.Match) -> str:
        day, clock, utc = match.group(1), match.group(2), match.group(3)
        if utc:
            return match.group(0)
        stamp = datetime.strptime(day + (clock or "T235959"), "%Y%m%dT%H%M%S")
        moment = stamp.replace(tzinfo=zone).astimezone(timezone.utc)
        return f"UNTIL={moment:%Y%m%dT%H%M%S}Z"

    return _UNTIL.sub(fix, rule)


def _occurrences(
    component: Any,
    start: datetime,
    window_start: datetime,
    window_end: datetime,
    *,
    zone: ZoneInfo | None = None,
    skip: list[datetime] | None = None,
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
        text = rrule_prop.to_ical().decode()
        rule = rrulestr(
            _utc_until(text, zone or start.tzinfo), dtstart=start, forceset=True
        )
        for moment in skip or []:
            rule.exdate(moment)
        # EXDATE: occurrences the organiser removed.
        exdates = component.get("EXDATE")
        for prop in exdates if isinstance(exdates, list) else ([exdates] if exdates else []):
            for excluded in getattr(prop, "dts", []):
                # A date-valued EXDATE cancels an all-day occurrence; it has
                # to become the same aware midnight the rule generates.
                moment, _ = _as_aware(getattr(excluded, "dt", None), start.tzinfo)
                if moment is not None:
                    rule.exdate(moment)
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
