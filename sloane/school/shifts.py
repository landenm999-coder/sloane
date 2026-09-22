"""Generate work shifts from the fixed rule.

Work does not post a schedule anywhere, so there is nothing to scrape. The rule
is 3-7 PM Monday to Friday and the job is to materialise it into rows that
"what am I doing Thursday" can answer exactly.

Two things this gets right that a naive loop does not:

* The hours are local. Building them in UTC and converting drifts by an hour
  either side of a daylight-saving change -- in Denver, 3 PM MDT is 21:00Z from
  March to November and 22:00Z the rest of the year. The times are constructed
  in Landen's timezone and converted, so a shift generated in September is
  still 3 PM local when November arrives.
* Nothing is invented on a day off. Weekends are skipped, and a shift already
  marked cancelled is never resurrected -- the generator only ever adds.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

WEEKDAYS = (0, 1, 2, 3, 4)  # Monday..Friday


def shift_times(
    day: date,
    *,
    tz: str,
    start_hour: int,
    end_hour: int,
) -> tuple[datetime, datetime]:
    """The UTC span of one day's shift, built from local wall-clock hours."""
    zone = ZoneInfo(tz)
    starts = datetime(day.year, day.month, day.day, start_hour, tzinfo=zone)
    ends = datetime(day.year, day.month, day.day, end_hour, tzinfo=zone)
    return starts, ends


def weekdays_from(start: date, weeks: int) -> Iterator[date]:
    """Every Mon-Fri date in the window, starting at `start`."""
    for offset in range(weeks * 7):
        day = start + timedelta(days=offset)
        if day.weekday() in WEEKDAYS:
            yield day


def planned_shifts(
    start: date,
    weeks: int,
    *,
    tz: str,
    start_hour: int,
    end_hour: int,
) -> list[tuple[datetime, datetime]]:
    """Every shift the rule implies over the window, in UTC."""
    return [
        shift_times(day, tz=tz, start_hour=start_hour, end_hour=end_hour)
        for day in weekdays_from(start, weeks)
    ]
