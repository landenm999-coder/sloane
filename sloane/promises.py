"""Promises he has made: "/promise send Keegan the outline by friday".

A promise is a commitment row (tier 4), so it shows up in FACTS, in the
briefs and in the weekly review until he marks it kept. Read by rules: the
"by ..." clause goes through the reminder parser, and "to Keegan" / "Keegan:"
names who it's owed to.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from sloane.reminders import parse as parse_when

_BY = re.compile(r"\s+by\s+(?!the\s+way\b)(.+)$", re.I)
_LEADING_NAME = re.compile(r"^\s*([A-Z][\w'’-]{1,30})\s*:\s*(.+)$", re.S)
_TO_NAME = re.compile(r"\b(?:to|for|with)\s+([A-Z][\w'’-]{1,30})\b")
_CLOCKISH = re.compile(r"\d|noon|midnight|morning|afternoon|evening|night|tonight", re.I)
# "By Friday" with no time means by the end of that day, not 7 AM.
END_OF_DAY = (20, 0)


@dataclass(frozen=True)
class Promise:
    what: str
    person: str | None
    due: datetime | None


def parse(text: str, now: datetime) -> Promise | None:
    text = " ".join((text or "").split())
    if not text or re.fullmatch(r"[\w'’-]+:", text):
        return None
    person = None
    lead = _LEADING_NAME.match(text)
    if lead:
        person, text = lead.group(1), lead.group(2).strip()

    due = None
    by = _BY.search(text)
    if by:
        when = by.group(1)
        parsed = parse_when(when, now)
        if parsed is not None and not parsed.text:
            due = parsed.due
            if not _CLOCKISH.search(when):
                end = due.replace(hour=END_OF_DAY[0], minute=END_OF_DAY[1])
                due = end if end > now else due
            text = text[: by.start()].strip()
    if not text:
        return None
    if person is None:
        named = _TO_NAME.search(text)
        person = named.group(1) if named else None
    return Promise(what=text, person=person, due=due)
