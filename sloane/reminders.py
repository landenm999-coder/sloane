"""Timed reminders: "remind me at 5 to call Keegan".

The time is read by rules, not by a model. A reminder is the one feature that
must work when every provider is down, and "at 5" meaning 5 PM to a student at
10 AM is a rule, not a judgement. What the rules cannot place, she asks about
rather than guesses: a reminder at the wrong time is worse than none.

Understood, anywhere in the text:

    in 20 minutes · in an hour · in half an hour · in 2 days
    at 5 · at 5:30 · 5pm · 7:15am · noon · midnight · 5 o'clock
    today · tonight · tomorrow (morning|afternoon|evening|night)
    friday · on fri · next friday (morning|...)

and number words for the voice-note path ("at five", "in ten minutes").
A bare hour with no am/pm is the next one that has not passed on that day.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta

WEEKDAYS = {
    "mon": 0, "monday": 0, "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "weds": 2, "wednesday": 2, "thu": 3, "thur": 3, "thurs": 3,
    "thursday": 3, "fri": 4, "friday": 4, "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}
# When a day is named with no time. Morning is before school; evening is after
# the 3-7 shift, when he is actually home to act on it.
PARTS = {"morning": time(7, 0), "afternoon": time(14, 0), "evening": time(19, 30),
         "night": time(20, 0), "tonight": time(20, 0)}
DEFAULT_DAY_TIME = time(7, 0)

_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15,
    "twenty": 20, "thirty": 30, "forty": 40, "forty-five": 45, "forty five": 45,
    "ninety": 90,
}
_UNITS = {
    "m": 1, "min": 1, "mins": 1, "minute": 1, "minutes": 1,
    "h": 60, "hr": 60, "hrs": 60, "hour": 60, "hours": 60,
    "d": 1440, "day": 1440, "days": 1440,
}

_WORDS = "|".join(sorted(map(re.escape, _NUMBER_WORDS), key=len, reverse=True))
_WORD_AFTER_AT_IN = re.compile(rf"\b(at|in)\s+({_WORDS})\b", re.I)
_WORD_BEFORE_MERIDIEM = re.compile(rf"\b({_WORDS})(\s*(?:am|pm|a\.m\.|p\.m\.|o'?clock))", re.I)
_WORD_MINUTES = re.compile(rf"(\d{{1,2}})\s+({_WORDS})\b(?=\s*(?:am|pm|a\.m\.|p\.m\.)?)", re.I)

_RELATIVE = re.compile(
    r"\bin\s+(half\s+an?|an?|\d+(?:\.\d+)?)\s*"
    r"(m|mins?|minutes?|h|hrs?|hours?|d|days?)\b",
    re.I,
)
_DAY = re.compile(
    r"\b(?:on\s+)?(next\s+)?(today|tonight|tomorrow|tmrw|tmr|"
    + "|".join(sorted(WEEKDAYS, key=len, reverse=True))
    + r")(?:\s+(morning|afternoon|evening|night))?\b",
    re.I,
)
_TIME = re.compile(
    r"(?:\bat\s+)?\b(noon|midnight|"
    r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.|o'?clock)"
    r"|(\d{1,2}):(\d{2}))(?![\w:])",
    re.I,
)
_BARE_AT = re.compile(r"\bat\s+(\d{1,2})(?::(\d{2}))?\b(?!\s*(?:%|st|nd|rd|th))", re.I)
_LEAD = re.compile(r"^(?:\s|,|:|-|to\b|that\b|me\b|about\b)+", re.I)
_TRAIL = re.compile(r"(?:\s|,|:|-|\bto\b)+$", re.I)


# "Remind me at 5 to call Keegan", as typed, spoken or captured. Group 1 is the
# rest. Shared by the chat and the Capture intake so both read it the same way.
REMIND_ME = re.compile(
    r"^\s*(?:hey\s+)?(?:sloane[,\s]+)?(?:please\s+)?remind\s+me\b[,:]?\s*(.*)$", re.I | re.S
)


# Buttons under a delivered reminder: "r:<uuid>:<code>", well under Telegram's
# 64-byte callback limit, and validated like any other input on the way back.
SNOOZE_CODES = {"10": "10 min", "60": "1 hour", "tom": "Tomorrow 7am", "ok": "Done"}
_SNOOZE = re.compile(r"^r:([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}):(10|60|tom|ok)$")


def snooze_data(reminder_id: str, code: str) -> str:
    return f"r:{reminder_id}:{code}"


def parse_snooze(data: str) -> tuple[str, str] | None:
    found = _SNOOZE.match(data or "")
    return (found.group(1), found.group(2)) if found else None


def snoozed_until(code: str, now: datetime) -> datetime | None:
    """When a snoozed reminder comes back. None for "Done"."""
    if code == "10":
        return (now + timedelta(minutes=10)).replace(second=0, microsecond=0)
    if code == "60":
        return (now + timedelta(hours=1)).replace(second=0, microsecond=0)
    if code == "tom":
        day = now.date() + timedelta(days=1)
        return now.replace(year=day.year, month=day.month, day=day.day,
                           hour=DEFAULT_DAY_TIME.hour, minute=DEFAULT_DAY_TIME.minute,
                           second=0, microsecond=0)
    return None


@dataclass(frozen=True)
class Parsed:
    due: datetime
    text: str


def _words_to_digits(text: str) -> str:
    """Only where a number word is plainly a time: "at five", "in ten", "six pm"."""
    text = _WORD_AFTER_AT_IN.sub(lambda m: f"{m.group(1)} {_NUMBER_WORDS[m.group(2).lower()]}", text)
    # "at 5 thirty" -> "at 5:30", before "thirty pm" can become "30 pm".
    text = _WORD_MINUTES.sub(
        lambda m: f"{m.group(1)}:{_NUMBER_WORDS[m.group(2).lower()]:02d}"
        if _NUMBER_WORDS[m.group(2).lower()] < 60 else m.group(0),
        text,
    )
    return _WORD_BEFORE_MERIDIEM.sub(lambda m: f"{_NUMBER_WORDS[m.group(1).lower()]}{m.group(2)}", text)


def _clock(hour: int, minute: int, meridiem: str | None) -> tuple[int, int, bool] | None:
    """(hour, minute, ambiguous). None if it is not a real time."""
    if minute > 59:
        return None
    mer = (meridiem or "").lower().replace(".", "")
    if mer in ("am", "pm"):
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if mer == "pm" else 0)
        return hour, minute, False
    if hour > 23:
        return None
    # "o'clock" and bare "at 5" are ambiguous unless it is already 24-hour.
    return hour, minute, 1 <= hour <= 12


def parse(text: str, now: datetime) -> Parsed | None:
    """When, and what, from free text. `now` must be aware and in his timezone.

    None when no time can be placed -- the caller asks rather than guessing.
    """
    work = _words_to_digits(" " + (text or "") + " ")
    spans: list[tuple[int, int]] = []

    rel = _RELATIVE.search(work)
    if rel:
        amount_raw = rel.group(1).lower()
        if amount_raw.startswith("half"):
            amount = 0.5
        elif amount_raw in ("a", "an"):
            amount = 1.0
        else:
            amount = float(amount_raw)
        minutes = amount * _UNITS[rel.group(2).lower()]
        if minutes <= 0:
            return None
        spans.append(rel.span())
        due = now + timedelta(minutes=minutes)
        return _finish(work, spans, due.replace(second=0, microsecond=0), now)

    day = _DAY.search(work)
    clock = _TIME.search(work)
    bare = None if clock else _BARE_AT.search(work)

    hour = minute = None
    ambiguous = False
    if clock:
        word = clock.group(1).lower()
        if word == "noon":
            hour, minute = 12, 0
        elif word == "midnight":
            hour, minute = 0, 0
        else:
            parsed = _clock(
                int(clock.group(2) or clock.group(5)),
                int(clock.group(3) or clock.group(6) or 0),
                clock.group(4) if clock.group(2) else None,
            )
            if parsed is None:
                return None
            hour, minute, ambiguous = parsed
        spans.append(clock.span())
    elif bare:
        parsed = _clock(int(bare.group(1)), int(bare.group(2) or 0), None)
        if parsed is None:
            return None
        hour, minute, ambiguous = parsed
        spans.append(bare.span())

    if day is None and hour is None:
        return None

    today = now.date()
    target = today
    part_time = None
    explicit_day = False
    if day:
        spans.append(day.span())
        word = day.group(2).lower()
        part = (day.group(3) or "").lower()
        if word == "tonight":
            part = part or "tonight"
        elif word in ("tomorrow", "tmrw", "tmr"):
            target = today + timedelta(days=1)
            explicit_day = True
        elif word in WEEKDAYS:
            ahead = (WEEKDAYS[word] - today.weekday()) % 7
            if day.group(1) and ahead == 0:
                ahead = 7
            target = today + timedelta(days=ahead)
            explicit_day = ahead > 0
        part_time = PARTS.get(part) if part else None

    if hour is None:
        chosen = part_time or DEFAULT_DAY_TIME
        hour, minute = chosen.hour, chosen.minute
        # "Tonight" said after 8 PM still means tonight: the next of 10 or 11 PM.
        if part_time == PARTS["tonight"] and target == today:
            for late in (hour, 22, 23):
                if now.replace(hour=late, minute=0, second=0, microsecond=0) > now:
                    hour, minute = late, 0
                    break

    def at(d, h):  # noqa: ANN001, ANN202
        return now.replace(year=d.year, month=d.month, day=d.day, hour=h,
                           minute=minute, second=0, microsecond=0)

    candidates = [hour]
    if ambiguous:
        # A bare hour is the next one that hasn't passed; on a named future day
        # or with "tonight"/"evening", prefer the afternoon/evening reading for
        # 1-6 (nobody means "at 5" as 5 AM) and the morning one for 7-11.
        if part_time is not None and part_time.hour >= 12:
            candidates = [hour % 12 + 12]
        elif explicit_day:
            candidates = [hour % 12 + 12] if 1 <= hour <= 6 else [hour % 12]
        else:
            candidates = sorted({hour % 12, hour % 12 + 12})
    due = None
    for h in candidates:
        moment = at(target, h)
        if moment > now:
            due = moment
            break
    if due is None:
        if day and day.group(2).lower() in WEEKDAYS:
            due = at(target + timedelta(days=7), candidates[0])
        elif not explicit_day and not (day and day.group(2).lower() in ("today", "tonight")):
            # "at 9" when 9 has gone today (both readings): tomorrow's first.
            due = at(target + timedelta(days=1), candidates[0])
        else:
            return None  # "today at 8am" at noon: in the past, so ask.
    if hour == 0 and minute == 0 and clock and clock.group(1).lower() == "midnight" and due <= now:
        due = due + timedelta(days=1)
    return _finish(work, spans, due, now)


def _finish(work: str, spans: list[tuple[int, int]], due: datetime, now: datetime) -> Parsed | None:
    if due <= now:
        return None
    chars = list(work)
    for start, end in spans:
        for i in range(start, end):
            chars[i] = " "
    rest = re.sub(r"\s+", " ", "".join(chars)).strip()
    rest = _TRAIL.sub("", _LEAD.sub("", rest)).strip()
    return Parsed(due=due, text=rest)


def spoken(due: datetime, now: datetime) -> str:
    """'at 5:00 PM', 'tomorrow at 7:00 AM', 'Friday at 3:30 PM', 'Oct 3 at 9:00 AM'."""
    hour = due.hour % 12 or 12
    clock = f"{hour}:{due.minute:02d} {'AM' if due.hour < 12 else 'PM'}"
    days = (due.date() - now.date()).days
    if days == 0:
        return f"at {clock}"
    if days == 1:
        return f"tomorrow at {clock}"
    if days < 7:
        return f"{due:%A} at {clock}"
    return f"{due:%b} {due.day} at {clock}"
