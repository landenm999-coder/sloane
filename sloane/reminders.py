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
    # A repeating reminder's rule (see `next_due`); None for a one-off.
    repeat: str | None = None


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
    A repeating one ("every weekday at 7") comes back with its rule in `repeat`
    and its first time in `due`.
    """
    work = _words_to_digits(" " + (text or "") + " ")
    spans: list[tuple[int, int]] = []

    rel = _RELATIVE.search(work)
    if rel is None:
        repeating = _repeating(work, now)
        if repeating is not None:
            return repeating
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


# -- repeating reminders ---------------------------------------------------------
#
# "every weekday at 7", "every monday and thursday at 6", "every night at 10",
# "every 2 hours", "on the 1st of every month", "daily", "mondays". The rule is
# stored with the reminder; delivering one makes the next (jobs/briefs.py).
# Rules: "days:0,1,2,3,4" (weekday numbers, Monday 0), "every:N" (every N days),
# "hours:N" (every N hours, never in quiet hours), "month:D" (day D, or the
# month's last day when it has no D).

ALL_DAYS = (0, 1, 2, 3, 4, 5, 6)
_DAY_NAMES = "|".join(sorted(WEEKDAYS, key=len, reverse=True))
_DAY_LIST = rf"(?:{_DAY_NAMES})s?(?:\s*(?:,|and|&|\+|/)\s*(?:and\s+)?(?:{_DAY_NAMES})s?)*"
_SMALL = {"other": 2, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}
_EVERY = re.compile(
    r"\bevery\s+(?:single\s+)?(?:"
    rf"(?P<days>{_DAY_LIST})"
    r"|(?P<n>\d{1,2}|other|two|three|four|five|six)\s+(?P<unit>hours?|hrs?|days?|weeks?)"
    r"|(?P<single>day|night|morning|afternoon|evening|weekday|weekend|weeknight|week|month|hour)"
    r")(?:\s+(?P<part>morning|afternoon|evening|night))?\b(?!['\u2019]s)",
    re.I,
)
_MONTHLY = re.compile(
    r"\b(?:on\s+)?(?:the\s+)?(?P<d1>\d{1,2})(?:st|nd|rd|th)?\s+of\s+every\s+month\b"
    r"|\b(?:every\s+month|monthly)\s+on\s+the\s+(?P<d2>\d{1,2})(?:st|nd|rd|th)?\b",
    re.I,
)
_PLURAL_DAYS = "|".join(f"{d}s" for d in ("monday", "tuesday", "wednesday", "thursday", "friday",
                                           "saturday", "sunday"))
_SHORTHAND = re.compile(
    rf"\b(?:on\s+)?(?P<word>daily|nightly|weekdays|weekends|weeknights|{_PLURAL_DAYS})\b"
    r"(?:\s+(?P<part>morning|afternoon|evening|night)s?)?"
    # Only as the when, never an adjective: "daily reading" isn't a repeat.
    r"(?=\s*(?:$|[.!?,;]|at\b|to\b|in\s+the\b|from\b|\d|remind\b))",
    re.I,
)


def _day_numbers(text: str) -> tuple[int, ...]:
    words = re.findall(rf"(?:{_DAY_NAMES})", text.lower())
    return tuple(sorted({WEEKDAYS[w] for w in words}))


def _rule_from(found: re.Match, today) -> tuple[str, str] | None:  # noqa: ANN001
    """(rule, part of day) from a matched phrase; None if it isn't one."""
    groups = found.groupdict()
    part = (groups.get("part") or "").lower()
    if groups.get("d1") or groups.get("d2"):
        day = int(groups.get("d1") or groups.get("d2"))
        return (f"month:{day}", part) if 1 <= day <= 31 else None
    if groups.get("word"):
        word = groups["word"].lower()
        if word in ("daily", "nightly"):
            return f"days:{','.join(map(str, ALL_DAYS))}", part or ("night" if word == "nightly" else "")
        if word in ("weekdays", "weeknights"):
            return "days:0,1,2,3,4", part or ("night" if word == "weeknights" else "")
        if word == "weekends":
            return "days:5,6", part
        return f"days:{WEEKDAYS[word[:-1]]}", part
    if groups.get("days"):
        return f"days:{','.join(map(str, _day_numbers(groups['days'])))}", part
    if groups.get("unit"):
        raw = groups["n"].lower()
        n = _SMALL[raw] if raw in _SMALL else int(raw)
        unit = groups["unit"].lower()
        if unit.startswith("h"):
            return (f"hours:{n}", "") if 1 <= n <= 12 else None
        days = n * (7 if unit.startswith("w") else 1)
        if not 1 <= days <= 90:
            return None
        return (f"days:{','.join(map(str, ALL_DAYS))}" if days == 1 else f"every:{days}"), part
    single = (groups.get("single") or "").lower()
    if single == "hour":
        return "hours:1", ""
    if single == "week":
        return f"days:{today.weekday()}", part
    if single == "month":
        return f"month:{today.day}", part
    if single == "weekday":
        return "days:0,1,2,3,4", part
    if single == "weeknight":
        return "days:0,1,2,3,4", part or "night"
    if single == "weekend":
        return "days:5,6", part
    return f"days:{','.join(map(str, ALL_DAYS))}", part or ("" if single == "day" else single)


def _repeating(work: str, now: datetime) -> Parsed | None:
    """A repeating reminder, if the text asks for one; None otherwise."""
    found = _MONTHLY.search(work) or _EVERY.search(work) or _SHORTHAND.search(work)
    if found is None:
        return None
    ruled = _rule_from(found, now.date())
    if ruled is None:
        return None
    rule, part = ruled
    spans = [found.span()]
    if rule.startswith("hours:"):
        due = (now + timedelta(hours=int(rule.split(":")[1]))).replace(second=0, microsecond=0)
        return _with_rule(_finish(work, spans, due, now), rule)

    clock = _TIME.search(work, 0)
    bare = None if clock else _BARE_AT.search(work)
    hour = minute = None
    ambiguous = False
    if clock:
        word = clock.group(1).lower()
        if word in ("noon", "midnight"):
            hour, minute = (12, 0) if word == "noon" else (0, 0)
        else:
            parsed = _clock(int(clock.group(2) or clock.group(5)), int(clock.group(3) or clock.group(6) or 0),
                            clock.group(4) if clock.group(2) else None)
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
    part_time = PARTS.get(part) if part else None
    if hour is None:
        chosen = part_time or DEFAULT_DAY_TIME
        hour, minute = chosen.hour, chosen.minute
    elif ambiguous:
        # Every day "at 5" is 5 PM; "at 9" is 9 AM; "every night at 9" is 9 PM.
        if part_time is not None and part_time.hour >= 12:
            hour = hour % 12 + 12
        elif part_time is None and hour != 12:
            hour = hour % 12 + 12 if 1 <= hour <= 6 else hour % 12
    due = first_due(rule, now.replace(hour=hour, minute=minute, second=0, microsecond=0), now)
    return _with_rule(_finish(work, spans, due, now), rule)


def first_due(rule: str, start: datetime, now: datetime) -> datetime:
    """The first time a rule allows at `start`'s time of day, from today on, still ahead."""
    kind, _, arg = rule.partition(":")
    if kind == "every":
        return start if start > now else _wall(start.date() + timedelta(days=1), start)
    if kind == "month":
        this = _wall(start.date().replace(day=_month_day(start.year, start.month, int(arg))), start)
        return this if this > now else next_due(rule, this)
    return start if start > now and _allows(rule, start) else next_due(rule, start)


def _with_rule(parsed: Parsed | None, rule: str) -> Parsed | None:
    return Parsed(due=parsed.due, text=parsed.text, repeat=rule) if parsed is not None else None


def _allows(rule: str, moment: datetime) -> bool:
    kind, _, arg = rule.partition(":")
    if kind == "days":
        return moment.weekday() in {int(d) for d in arg.split(",") if d}
    if kind == "month":
        return moment.day == _month_day(moment.year, moment.month, int(arg))
    return True


def _month_day(year: int, month: int, wanted: int) -> int:
    import calendar

    return min(wanted, calendar.monthrange(year, month)[1])


def _wall(day, moment: datetime) -> datetime:  # noqa: ANN001
    """`moment`'s wall-clock time on `day`, in its zone (DST-correct)."""
    return datetime.combine(day, time(moment.hour, moment.minute), tzinfo=moment.tzinfo)


def next_due(rule: str, after: datetime, *, quiet: tuple[time, time] = (time(0, 0), time(6, 30))) -> datetime:
    """The occurrence after `after` (aware, in his zone), at the same time of day.

    An hourly rule skips quiet hours: the one that would land in them comes at
    their end instead.
    """
    kind, _, arg = rule.partition(":")
    if kind == "hours":
        nxt = after + timedelta(hours=int(arg))
        start, end = quiet
        clock = nxt.timetz().replace(tzinfo=None)
        inside = start <= clock < end if start <= end else (clock >= start or clock < end)
        if inside:
            day = nxt.date() if clock < end else nxt.date() + timedelta(days=1)
            nxt = datetime.combine(day, end, tzinfo=after.tzinfo)
        return nxt
    if kind == "every":
        return _wall(after.date() + timedelta(days=int(arg)), after)
    if kind == "month":
        year, month = (after.year + 1, 1) if after.month == 12 else (after.year, after.month + 1)
        from datetime import date as _date

        return _wall(_date(year, month, _month_day(year, month, int(arg))), after)
    days = {int(d) for d in arg.split(",") if d} or set(ALL_DAYS)
    day = after.date() + timedelta(days=1)
    while day.weekday() not in days:
        day += timedelta(days=1)
    return _wall(day, after)


_DAY_WORDS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def repeat_spoken(rule: str, due: datetime) -> str:
    """'every weekday at 7:00 AM', 'every Monday and Thursday at 6:00 PM', 'every 2 hours'."""
    kind, _, arg = rule.partition(":")
    hour = due.hour % 12 or 12
    clock = f"{hour}:{due.minute:02d} {'AM' if due.hour < 12 else 'PM'}"
    if kind == "hours":
        return "every hour" if arg == "1" else f"every {arg} hours"
    if kind == "every":
        n = int(arg)
        when = f"every {n // 7} weeks" if n % 7 == 0 and n > 7 else ("every week" if n == 7 else f"every {n} days")
        return f"{when} at {clock}"
    if kind == "month":
        n = int(arg)
        suffix = "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
        return f"on the {n}{suffix} of every month at {clock}"
    days = sorted({int(d) for d in arg.split(",") if d})
    if days == list(ALL_DAYS):
        which = "every day"
    elif days == [0, 1, 2, 3, 4]:
        which = "every weekday"
    elif days == [5, 6]:
        which = "every weekend day"
    else:
        names = [_DAY_WORDS[d] for d in days]
        which = "every " + (names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1])
    return f"{which} at {clock}"


# -- timers --------------------------------------------------------------------------
#
# "set a timer for 10 minutes", "timer 25 min", "10 minute timer for the pasta".
# A timer is a reminder with the time up front: the same delivery, the same
# snooze buttons, and it shows in /reminders.
TIMER = re.compile(
    r"^\s*(?:hey\s+)?(?:sloane[,\s]+)?(?:please\s+)?(?:(?:set|start)\s+(?:a|an|the)?\s*)?"
    r"(?:(?:timer\s+(?:for\s+)?(?P<n1>\d+(?:\.\d+)?|an?|half\s+an?)\s*(?P<u1>s|secs?|seconds?|m|mins?|minutes?|h|hrs?|hours?))"
    r"|(?:(?P<n2>\d+(?:\.\d+)?)[\s-]*(?P<u2>s|secs?|seconds?|m|mins?|minutes?|h|hrs?|hours?)\s+timer))"
    r"(?:\s+(?:for|to|on)\s+(?P<what>.+?))?\s*[.!]*\s*$",
    re.I,
)
MAX_TIMER_MINUTES = 12 * 60


def parse_timer(text: str, now: datetime) -> Parsed | None:
    """A timer: when it's up, and what it's for ('' if nothing). None if it isn't one."""
    found = TIMER.match(_words_to_digits(" " + (text or "") + " ").strip() or "")
    if found is None:
        # Number words after "for": "set a timer for ten minutes".
        spelled = re.sub(rf"\bfor\s+({_WORDS})\b", lambda m: f"for {_NUMBER_WORDS[m.group(1).lower()]}", text or "",
                         flags=re.I)
        found = TIMER.match(spelled)
        if found is None:
            return None
    raw = (found["n1"] or found["n2"]).lower()
    amount = 0.5 if raw.startswith("half") else (1.0 if raw in ("a", "an") else float(raw))
    unit = (found["u1"] or found["u2"]).lower()
    minutes = amount / 60 if unit.startswith("s") else amount * (60 if unit.startswith("h") else 1)
    if not 0 < minutes <= MAX_TIMER_MINUTES:
        return None
    due = now + timedelta(seconds=round(minutes * 60))
    return Parsed(due=due.replace(microsecond=0), text=(found["what"] or "").strip())
