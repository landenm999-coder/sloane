"""Countdowns: a named day and how far off it is.

    /countdown graduation may 22      ·   /countdown DECA districts on dec 3
    /countdown                        ·   /countdown drop 2
    "how many days until graduation?" ·   "how long until prom?"

Every countdown still ahead is a FACTS line, so a brief can say "prom is in
nine days" from a row rather than from memory. He hears about each one a week
out, the day before and on the day, through the heartbeat. The date is read by
`sloane.dates`, never by a model.
"""

from __future__ import annotations

import re
from datetime import date

from sloane import dates
from sloane.ingest import safe_field
from sloane.skills import Answer, Nudge, Skill, SkillContext

MAX_NAME = 80
# Heads-up days before each countdown: a week out, the day before, the day.
HEADS_UP = {7: "One week until {name} ({when}).", 1: "Tomorrow: {name}.", 0: "Today: {name}."}

_HOW_LONG = re.compile(
    r"^\s*(?:how\s+many\s+(?:more\s+)?days\s+(?:until|till|til|to|before)|how\s+long\s+(?:until|till|til|before)|"
    r"days\s+(?:until|till|til|to))\s+(?:the\s+|my\s+)?(?P<name>.+?)\s*[?.!]*\s*$",
    re.I,
)


def _days(n: int) -> str:
    return f"{n} day{'s' if n != 1 else ''}"


def _words(text: str) -> set[str]:
    return set(re.findall(r"\w+", text.lower())) - {"the", "my", "a", "an"}


class Countdowns(Skill):
    name = "countdowns"
    help = ("`/countdown graduation may 22` — count down to a day; `/countdown` lists them",)
    commands = frozenset({"countdown", "countdowns"})

    async def _upcoming(self) -> list[dict]:
        return await self.ctx.store.upcoming_countdowns(self.ctx.today())

    def _line(self, row: dict, today: date) -> str:
        left = (row["on_date"] - today).days
        when = dates.spoken(row["on_date"], today)
        if left == 0:
            return f"{row['name']}: today"
        if left == 1:
            return f"{row['name']}: tomorrow"
        return f"{row['name']}: {_days(left)} ({when})"

    async def listing(self) -> Answer:
        rows = await self._upcoming()
        today = self.ctx.today()
        if not rows:
            return Answer("No countdowns set.", "Try `/countdown graduation may 22`.")
        lines = [f"{i}. {self._line(r, today)}" for i, r in enumerate(rows, 1)]
        first = rows[0]
        left = (first["on_date"] - today).days
        soonest = "today" if left == 0 else ("tomorrow" if left == 1 else f"in {_days(left)}")
        return Answer(f"{first['name'].capitalize()} is {soonest}"
                      + (f", and {len(rows) - 1} more after it." if len(rows) > 1 else "."),
                      "\n".join(lines) + "\n\n`/countdown drop <n>` removes one.")

    async def command(self, name: str, rest: str) -> Answer | None:
        rest = rest.strip()
        if not rest:
            return await self.listing()
        parts = rest.split()
        if parts[0].lower() in {"drop", "remove", "done", "cancel"} and len(parts) == 2 and parts[1].isdigit():
            rows = await self._upcoming()
            n = int(parts[1])
            if not 1 <= n <= len(rows):
                return Answer("Use /countdown drop with a number from /countdown.")
            row = await self.ctx.store.archive_countdown(str(rows[n - 1]["id"]))
            if row is None:
                return Answer("That one's already gone.")
            return Answer(f"Dropped the countdown to {row['name']}.")
        today = self.ctx.today()
        found = dates.find(rest, today)
        if found is None:
            return Answer("I couldn't tell the date.", "Try `/countdown prom april 18` or `/countdown trip 6/12`.")
        what = safe_field(dates.remove(rest, found), limit=MAX_NAME)
        if not what:
            return Answer("What are we counting down to?", "Try `/countdown prom april 18`.")
        if found.day < today:
            return Answer(f"{dates.spoken(found.day, today)} has already passed.")
        await self.ctx.store.add_countdown(what, found.day)
        left = (found.day - today).days
        away = f", {_days(left)} away" if left > 1 else ""
        return Answer(f"Counting down to {what}: {dates.spoken(found.day, today)}{away}.")

    async def match(self, text: str) -> Answer | None:
        found = _HOW_LONG.match(text.replace("’", "'"))
        if found is None:
            return None
        wanted = _words(found["name"])
        if not wanted:
            return None
        rows = [r for r in await self._upcoming() if wanted <= _words(r["name"])]
        if len(rows) != 1:
            return None  # not one of ours, or ambiguous: the agent has the FACTS lines
        row, today = rows[0], self.ctx.today()
        left = (row["on_date"] - today).days
        if left == 0:
            return Answer(f"{row['name'].capitalize()} is today.")
        return Answer(f"{_days(left).capitalize()} until {row['name']}, on {dates.spoken(row['on_date'], today)}.")

    async def facts(self) -> list[str]:
        today = self.ctx.today()
        lines = []
        for r in await self._upcoming():
            left = (r["on_date"] - today).days
            away = "today" if left == 0 else ("tomorrow" if left == 1 else f"{_days(left)} away")
            lines.append(f"- COUNTDOWN {safe_field(r['name'], limit=MAX_NAME)}: "
                         f"{r['on_date']:%a %b} {r['on_date'].day}, {r['on_date'].year} ({away})")
        return lines

    async def panel(self) -> dict | None:
        rows = await self._upcoming()
        if not rows:
            return None
        today = self.ctx.today()
        return {
            "title": "Countdowns",
            "lines": [self._line(r, today) for r in rows[:8]],
            "items": [{"name": r["name"], "date": r["on_date"].isoformat(),
                       "days": (r["on_date"] - today).days} for r in rows],
        }

    async def nudges(self) -> list[Nudge]:
        today = self.ctx.today()
        out = []
        for r in await self._upcoming():
            left = (r["on_date"] - today).days
            if left in HEADS_UP:
                text = HEADS_UP[left].format(name=r["name"], when=dates.spoken(r["on_date"], today))
                out.append(Nudge(f"countdown:{r['id']}:{left}", "📅 " + text))
        return out


def build(ctx: SkillContext) -> Skill:
    return Countdowns(ctx)
