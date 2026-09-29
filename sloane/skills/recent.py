"""Recent: what he just captured, newest first. Reads only; it changes nothing.

    "what did I just capture?"  ·  "what did I just write down?"
    "check what I just wrote down on capture"       the latest one
    "last 5 captures"  ·  "my recent captures"       the last few
    "what did I capture today?"  ·  /recent today    everything since midnight
    /recent  ·  /recent 5

It looks across everything he saves by hand: /idea, /remember, /followup, /list,
/promise, /remind and /spent. Her own ideas, what she learns overnight, timers and
snoozes are not his captures, so they are left out. "last 5" alone is not
enough (it could mean anything); the message has to be about a capture.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from sloane.ingest import safe_field
from sloane.skills import Answer, Skill, SkillContext

DEFAULT = 1
MAX_SHOWN = 20
TODAY_LIMIT = 50
MAX_TEXT = 100

_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
          "ten": 10}
_NUM = r"(?P<n>\d{1,2}|" + "|".join(_WORDS) + r")"
_SAVED = r"(?:captur\w*|write(?:\s+down)?|wrote(?:\s+down)?|jot\w*(?:\s+down)?|sav(?:e|ed)|noted?)"
# "what did I just capture", "check what I just wrote down on capture"
_JUST = re.compile(
    r"^\s*(?:(?:can|could)\s+you\s+)?(?:please\s+)?(?:just\s+)?(?:(?:check|see|show(?:\s+me)?|tell\s+me|read\s+back)\s+)?"
    r"what\s+(?:did\s+i|i(?:'ve|\s+have)?)\s+just\s+" + _SAVED + r"\b.*$",
    re.I,
)
# "last 5 captures", "my recent captures", "what did I capture today"
_CAPTURES = re.compile(
    r"^\s*(?:(?:show|check|see|list|read)(?:\s+me)?\s+)?(?:(?:my|the)\s+)?(?:(?:latest|recent|last)(?:\s+" + _NUM + r")?\s+captures?"
    r"|captures?\s+(?:from\s+)?today|what\s+(?:did\s+i|have\s+i)\s+captur\w*\s+today)\b.*$",
    re.I,
)
_LAST = re.compile(r"\b(?:last|latest|recent)\s+" + _NUM + r"\b", re.I)
_TODAY = re.compile(r"\btoday\b", re.I)

_KIND = {
    "idea": "/idea → workshop",
    "remember": "/remember → memory",
    "followup": "/followup → loose ends",
    "list": "/list",
    "promise": "/promise → promises",
    "remind": "/remind → reminders",
    "spent": "/spent",
}


def window(text: str) -> tuple[int, bool]:
    """(how many, since midnight?) from his words: 'last 5' -> (5, False), 'today' -> (TODAY_LIMIT, True)."""
    if _TODAY.search(text):
        return TODAY_LIMIT, True
    found = _LAST.search(text) or re.search(r"^\s*" + _NUM + r"\s*$", text)
    if found is None:
        return DEFAULT, False
    raw = found["n"].lower()
    n = int(raw) if raw.isdigit() else _WORDS[raw]
    return max(1, min(n, MAX_SHOWN)), False


def _ago(at: datetime, now: datetime) -> str:
    secs = max(0, int((now - at).total_seconds()))
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60} min ago"
    clock = f"{at.hour % 12 or 12}:{at.minute:02d} {'AM' if at.hour < 12 else 'PM'}"
    if at.date() == now.date():
        return f"today {clock}"
    if at.date() == now.date() - timedelta(days=1):
        return f"yesterday {clock}"
    return f"{at.strftime('%b')} {at.day} {clock}"


def where(row: dict) -> str:
    """'/list → grocery list', '/spent → food $12', '/idea → workshop': the command and where it went."""
    kind, place = row["kind"], row.get("place")
    if kind == "list":
        return f"/list → {safe_field(str(place), limit=40)} list"
    if kind == "spent":
        category, _, cents = str(place).rpartition(" ")
        dollars = int(cents) / 100
        amount = f"${dollars:,.0f}" if dollars == int(dollars) else f"${dollars:,.2f}"
        return f"/spent → {category} {amount}"
    return _KIND[kind]


def line(row: dict, now: datetime) -> str:
    at = row["at"].astimezone(now.tzinfo)
    return f"{_ago(at, now)} · {where(row)}: \"{safe_field(str(row['what']), limit=MAX_TEXT)}\""


class Recent(Skill):
    name = "recent"
    help = (
        "`/recent` · `/recent 5` · `/recent today` — what you just captured (or ask \"what did I just capture?\")",
    )
    commands = frozenset({"recent"})

    async def show(self, count: int, today: bool) -> Answer:
        now = self.ctx.now()
        since = now.replace(hour=0, minute=0, second=0, microsecond=0) if today else None
        rows = await self.ctx.store.recent_captures(count, since)
        if not rows:
            return Answer("Nothing captured today." if today else "You haven't captured anything yet.")
        lines = [line(r, now) for r in rows]
        if len(rows) == 1 and not today:
            return Answer(f"Latest: {lines[0]}")
        noun = f"{len(rows)} capture{'s' if len(rows) != 1 else ''}"
        head = f"{noun} today" if today else f"Your last {noun}"
        return Answer(f"{head}. Newest first: {lines[0]}", "\n".join(f"{i}. {t}" for i, t in enumerate(lines, 1)))

    async def command(self, name: str, rest: str) -> Answer | None:
        count, today = window(rest)
        return await self.show(count, today)

    async def match(self, text: str) -> Answer | None:
        text = text.replace("’", "'")
        if _JUST.match(text) or _CAPTURES.match(text):
            return await self.show(*window(text))
        return None


def build(ctx: SkillContext) -> Skill:
    return Recent(ctx)
