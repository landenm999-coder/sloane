"""Focus sessions: a timer for one thing, and a tally of the time.

    /focus 25 physics lab    25 minutes on physics lab (25 if no number)
    /focus                   what's running, or today's and this week's total
    /focus stop              end early; the time so far still counts

The "time's up" message is an ordinary reminder, so it is delivered by the
reminders job (held through quiet hours, retried if Telegram is down) and
shows in /reminders. Stopping early cancels it. One session runs at a time;
starting another ends the one before. The day's total is a FACTS line, so the
wrap brief can say "you put in 90 minutes on the essay today".
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from sloane.ingest import safe_field
from sloane.skills import Answer, Skill, SkillContext

DEFAULT_MINUTES = 25
MAX_MINUTES = 240

# Longest unit first, and a word boundary: "25 math" keeps its m.
_START = re.compile(
    r"^\s*(?:(?P<n>\d{1,3})\s*(?:(?:minutes|minute|mins|min|m)\b)?\s*)?(?:on\s+|for\s+)?(?P<what>.*)$", re.I
)


def _clock(moment: datetime) -> str:
    hour = moment.hour % 12 or 12
    return f"{hour}:{moment.minute:02d} {'AM' if moment.hour < 12 else 'PM'}"


def _minutes(n: int) -> str:
    hours, mins = divmod(n, 60)
    if hours:
        return f"{hours}h {mins:02d}m" if mins else f"{hours}h"
    return f"{mins} min"


class Focus(Skill):
    name = "focus"
    help = ("`/focus 25 essay` — a focus timer; `/focus` your time today; `/focus stop`",)
    commands = frozenset({"focus"})

    def _spent(self, row: dict, now: datetime) -> int:
        end = min(row["ends_at"], row["stopped_at"] or now, now)
        return max(0, int((end - row["started_at"]).total_seconds() // 60))

    async def _tally(self, since: datetime) -> tuple[int, dict[str, int]]:
        now = self.ctx.now()
        by_what: dict[str, int] = {}
        for row in await self.ctx.store.focus_since(since):
            spent = self._spent(row, now)
            if spent:
                by_what[row["what"]] = by_what.get(row["what"], 0) + spent
        return sum(by_what.values()), by_what

    def _midnight(self) -> datetime:
        return self.ctx.now().replace(hour=0, minute=0, second=0, microsecond=0)

    async def start(self, rest: str) -> Answer:
        found = _START.match(rest)
        minutes = int(found["n"]) if found and found["n"] else DEFAULT_MINUTES
        if not 1 <= minutes <= MAX_MINUTES:
            return Answer(f"Pick between 1 and {MAX_MINUTES} minutes.")
        what = safe_field((found["what"] if found else "").strip(" .!"), limit=80) or "focus"
        now = self.ctx.now().replace(second=0, microsecond=0)
        ended = await self._stop_running(now)
        ends = now + timedelta(minutes=minutes)
        reminder = await self.ctx.store.add_reminder(
            text=f"⏱ Time's up: {what} ({_minutes(minutes)}). Take five.", due_at=ends, source="focus")
        await self.ctx.store.start_focus(what=what, minutes=minutes, started_at=now, ends_at=ends,
                                         reminder_id=str(reminder["id"]))
        before = f" (ended {ended['what']} first)" if ended else ""
        return Answer(f"Focusing on {what} for {_minutes(minutes)}; I'll tell you at {_clock(ends)}.{before}")

    async def _stop_running(self, now: datetime) -> dict | None:
        row = await self.ctx.store.running_focus(now)
        if row is None:
            return None
        await self.ctx.store.stop_focus(str(row["id"]), now)
        if row.get("reminder_id"):
            await self.ctx.store.cancel_reminder(str(row["reminder_id"]))
        return row

    async def stop(self) -> Answer:
        now = self.ctx.now()
        row = await self._stop_running(now)
        if row is None:
            return Answer("No focus session is running.")
        spent = self._spent({**row, "stopped_at": now}, now)
        total, _ = await self._tally(self._midnight())
        return Answer(f"Stopped {row['what']} after {_minutes(spent)}. {_minutes(total)} today.")

    async def status(self) -> Answer:
        now = self.ctx.now()
        running = await self.ctx.store.running_focus(now)
        today, by_what = await self._tally(self._midnight())
        week_start = self._midnight() - timedelta(days=self._midnight().weekday())
        week, _ = await self._tally(week_start)
        lines = [f"• {what}: {_minutes(m)}" for what, m in sorted(by_what.items(), key=lambda kv: -kv[1])]
        lines.append(f"This week: {_minutes(week)}")
        if running is not None:
            left = max(1, int((running["ends_at"] - now).total_seconds() // 60))
            return Answer(f"{_minutes(left)} left on {running['what']}. {_minutes(today)} today so far.",
                          "\n".join(lines))
        if not today:
            return Answer("No focus time yet today.", f"This week: {_minutes(week)}\n\n`/focus 25 <what>` starts one.")
        return Answer(f"{_minutes(today)} of focus today.", "\n".join(lines))

    async def command(self, name: str, rest: str) -> Answer | None:
        rest = rest.strip()
        if not rest:
            return await self.status()
        if rest.lower() in {"stop", "end", "done", "cancel"}:
            return await self.stop()
        return await self.start(rest)

    async def facts(self) -> list[str]:
        now = self.ctx.now()
        today, by_what = await self._tally(self._midnight())
        running = await self.ctx.store.running_focus(now)
        if not today and running is None:
            return []
        each = ", ".join(f"{safe_field(w, limit=40)} {m}" for w, m in by_what.items())
        line = f"- FOCUS today: {_minutes(today)}" + (f" ({each})" if each else "")
        if running is not None:
            line += f"; running now: {safe_field(running['what'], limit=40)} until {_clock(running['ends_at'].astimezone(now.tzinfo))}"
        return [line]

    async def panel(self) -> dict | None:
        now = self.ctx.now()
        today, _ = await self._tally(self._midnight())
        running = await self.ctx.store.running_focus(now)
        if not today and running is None:
            return None
        lines = [f"{_minutes(today)} today"]
        if running is not None:
            lines.insert(0, f"Now: {running['what']} until {_clock(running['ends_at'].astimezone(now.tzinfo))}")
        return {"title": "Focus", "lines": lines}


def build(ctx: SkillContext) -> Skill:
    return Focus(ctx)
