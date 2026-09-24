"""Dates from his words, by rules: "may 22", "5/22", "friday", "in 3 weeks".

The day-level sibling of `reminders.parse`, which places a time. This is for the
things where the day is all that matters -- a countdown, a birthday, a client
follow-up -- and, like reminders, it never asks a model: "the 30th" is a rule,
and a wrong date is worse than a question.

Understood, anywhere in the text (first match in this order wins):

    2027-05-22 · 5/22 · 5/22/27 · 5/22/2027         (US month/day)
    may 22 · may 22nd · may 22, 2027 · 22 may · the 22nd of may
    the 30th                                         (that day, this month or next)
    in 3 days · in two weeks · in a month · in a year
    today · tonight · tomorrow · day after tomorrow · yesterday
    friday · this friday · next friday · on fri      ("sat", "sun" and "wed" need
                                                     "on", "this", "next" or "by")

A date with no year is the next one that has not passed (or, with
`future=False`, the last one that has). An impossible date ("feb 30") is no
date at all, so the caller asks.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, timedelta

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2,
    "wed": 2, "weds": 2, "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}
# Abbreviations that are also ordinary words ("the sun", "sat down", "wed").
_NEEDS_QUALIFIER = {"sat", "sun", "wed"}
_NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12,
}

_MONTH = "|".join(sorted(MONTHS, key=len, reverse=True))
_WEEKDAY = "|".join(sorted(WEEKDAYS, key=len, reverse=True))
_NUMBER = "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True))
_ORD = r"(?:st|nd|rd|th)?"

_ISO = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
_SLASH = re.compile(r"(?<![\d/])(\d{1,2})/(\d{1,2})(?:/(\d{4}|\d{2}))?(?![\d/])")
_MONTH_DAY = re.compile(
    rf"\b({_MONTH})\.?\s+(\d{{1,2}}){_ORD}\b(?:,?\s+(\d{{4}})\b)?", re.I
)
_DAY_MONTH = re.compile(
    rf"\b(?:the\s+)?(\d{{1,2}}){_ORD}\s+(?:of\s+)?({_MONTH})\b\.?(?:,?\s+(\d{{4}})\b)?", re.I
)
_ORDINAL = re.compile(r"\b(?:the\s+)?(\d{1,2})(st|nd|rd|th)\b(?!\s+(?:of\s+)?(?:" + _MONTH + r")\b)", re.I)
_RELATIVE = re.compile(rf"\bin\s+(\d+|{_NUMBER})\s+(day|week|month|year)s?\b", re.I)
_NAMED = re.compile(r"\b(day after tomorrow|today|tonight|tomorrow|tmrw|tmr|yesterday)\b", re.I)
_WEEKDAY_RE = re.compile(rf"\b(?:(on|this|next|by|until|till|before)\s+)?({_WEEKDAY})\b\.?", re.I)

# Connecting words that belong to the date, not to what's left: "graduation
# is on may 22" leaves "graduation", not "graduation is on".
_LEAD = re.compile(r"(?:\b(?:is|are|on|by|for|due|until|till|before|at|from)\b[\s,:-]*)+$", re.I)
_EDGES = re.compile(r"^[\s,:;.-]+|[\s,:;-]+$")


@dataclass(frozen=True)
class Found:
    day: date
    start: int
    end: int
    # The words named a year ("may 22, 2027"). A birthday ignores the year
    # either way; a countdown needs to know it was not guessed.
    year_given: bool = False


def _safe(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _next_or_last(month: int, day: int, today: date, future: bool) -> date | None:
    """This year's month/day, or the adjacent year's if it is on the wrong side of today."""
    if not 1 <= month <= 12 or not 1 <= day <= 31:
        return None
    for year in ((today.year, today.year + 1) if future else (today.year, today.year - 1)):
        found = _safe(year, month, day)
        if found is None:
            continue  # Feb 29 in a common year: try the other
        if (future and found >= today) or (not future and found <= today):
            return found
    # Feb 29 with no leap year either side, or an impossible date.
    for offset in range(2, 9):
        year = today.year + (offset if future else -offset)
        found = _safe(year, month, day)
        if found is not None:
            return found
    return None


def _add_months(start: date, months: int) -> date:
    month_index = start.month - 1 + months
    year = start.year + month_index // 12
    month = month_index % 12 + 1
    return date(year, month, min(start.day, calendar.monthrange(year, month)[1]))


def _year(raw: str | None) -> int | None:
    if not raw:
        return None
    value = int(raw)
    return 2000 + value if value < 100 else value


def find(text: str, today: date, *, future: bool = True) -> Found | None:
    """The first date in `text`, or None. Never raises."""
    text = text or ""

    iso = _ISO.search(text)
    if iso:
        found = _safe(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        return Found(found, *iso.span(), year_given=True) if found else None

    slash = _SLASH.search(text)
    if slash:
        month, day, year = int(slash.group(1)), int(slash.group(2)), _year(slash.group(3))
        found = _safe(year, month, day) if year else _next_or_last(month, day, today, future)
        return Found(found, *slash.span(), year_given=year is not None) if found else None

    for pattern, month_group, day_group in ((_MONTH_DAY, 1, 2), (_DAY_MONTH, 2, 1)):
        hit = pattern.search(text)
        if hit:
            month = MONTHS[hit.group(month_group).lower()]
            day = int(hit.group(day_group))
            year = _year(hit.group(3))
            found = _safe(year, month, day) if year else _next_or_last(month, day, today, future)
            return Found(found, *hit.span(), year_given=year is not None) if found else None

    ordinal = _ORDINAL.search(text)
    if ordinal:
        day = int(ordinal.group(1))
        if not 1 <= day <= 31:
            return None
        # This month if it has that day and it is on the right side of today,
        # else the nearest month in that direction that has it.
        for step in range(0, 13):
            base = _add_months(today.replace(day=1), step if future else -step)
            found = _safe(base.year, base.month, day)
            if found and ((future and found >= today) or (not future and found <= today)):
                return Found(found, *ordinal.span())
        return None

    relative = _RELATIVE.search(text)
    if relative:
        raw = relative.group(1).lower()
        amount = int(raw) if raw.isdigit() else _NUMBER_WORDS[raw]
        unit = relative.group(2).lower()
        if unit == "day":
            found = today + timedelta(days=amount)
        elif unit == "week":
            found = today + timedelta(weeks=amount)
        elif unit == "month":
            found = _add_months(today, amount)
        else:
            found = _add_months(today, 12 * amount)
        return Found(found, *relative.span())

    named = _NAMED.search(text)
    if named:
        word = named.group(1).lower()
        offset = {"today": 0, "tonight": 0, "tomorrow": 1, "tmrw": 1, "tmr": 1,
                  "day after tomorrow": 2, "yesterday": -1}[word]
        return Found(today + timedelta(days=offset), *named.span())

    for hit in _WEEKDAY_RE.finditer(text):
        qualifier = (hit.group(1) or "").lower()
        word = hit.group(2).lower()
        if word in _NEEDS_QUALIFIER and not qualifier:
            continue
        target = WEEKDAYS[word]
        if future:
            ahead = (target - today.weekday()) % 7
            if qualifier == "next" and ahead == 0:
                ahead = 7
            found = today + timedelta(days=ahead)
        else:
            found = today - timedelta(days=(today.weekday() - target) % 7)
        return Found(found, *hit.span())
    return None


def remove(text: str, found: Found) -> str:
    """The text with the date and its connecting words taken out."""
    before = _LEAD.sub("", text[: found.start].rstrip())
    rest = f"{before} {text[found.end:]}"
    return _EDGES.sub("", re.sub(r"\s+", " ", rest)).strip()


def spoken(day: date, today: date) -> str:
    """'today', 'tomorrow', 'Friday', 'Fri Oct 3', 'Sat May 22, 2027'."""
    days = (day - today).days
    if days == 0:
        return "today"
    if days == 1:
        return "tomorrow"
    if days == -1:
        return "yesterday"
    if 1 < days < 7:
        return f"{day:%A}"
    if day.year != today.year and abs(days) > 60:
        return f"{day:%a %b} {day.day}, {day.year}"
    return f"{day:%a %b} {day.day}"


def until(day: date, today: date) -> str:
    """'today', 'tomorrow', 'in 9 days', '3 days ago'."""
    days = (day - today).days
    if days == 0:
        return "today"
    if days == 1:
        return "tomorrow"
    if days == -1:
        return "yesterday"
    return f"in {days} days" if days > 0 else f"{-days} days ago"
