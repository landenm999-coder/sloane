"""A study plan for tonight: his free time, filled with what's due soonest.

    /plan  ·  /plan tomorrow  ·  "plan my night"  ·  "what's my plan for tonight?"
    /estimate lab writeup 2h      how long one will take (else a guess from the title)

Computed, never inferred. Free time is the evening (from the end of school, or
from the morning on a weekend, until bedtime) minus his shift with a commute
either side, minus anything on the calendar. Work due in the next four days,
overdue first, then soonest due, is placed into that time in order. What
doesn't fit is said plainly, with when it's due, so the choice is his.

One FACTS line says how much time is free and how much work is waiting, so
the post-shift brief can say "two hours free, three hours of work" from rows.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sloane.ingest import safe_field
from sloane.skills import Answer, Skill, SkillContext

HORIZON_DAYS = 4
MIN_CHUNK = 15      # minutes: less than this isn't worth starting something in
BREAK = 10          # minutes between two pieces of work in one stretch
DEFAULT_MINUTES = 30

# First match wins, so "final essay" is an essay, not a test.
_GUESSES: tuple[tuple[re.Pattern, int], ...] = (
    (re.compile(r"\b(?:essay|paper|project|presentation|portfolio|research)\b", re.I), 90),
    (re.compile(r"\b(?:test|exam|midterm|final|quiz)\b", re.I), 60),
    (re.compile(r"\b(?:lab|report|write-?up|analysis)\b", re.I), 60),
    (re.compile(r"\b(?:read|reading|chapter|article)\b", re.I), 40),
    (re.compile(r"\b(?:worksheet|homework|hw|practice|problems|questions|review)\b", re.I), 30),
)
_DURATION = re.compile(
    r"(?:(?P<h>\d+(?:\.\d+)?)\s*(?:h|hr|hrs|hour|hours))?\s*(?:(?P<m>\d+)\s*(?:m|min|mins|minute|minutes))?\s*$",
    re.I,
)
_ASK = re.compile(
    r"^\s*(?:(?:make\s+(?:me\s+)?a|what'?s\s+(?:my|the)|give\s+me\s+a)\s+)?(?:study\s+)?plan\s+"
    r"(?:for\s+)?(?:my\s+)?(?P<when>tonight|today|tomorrow|night|evening|day)\s*[?.!]*\s*$"
    r"|^\s*plan\s+my\s+(?P<when2>night|evening|day|tomorrow)\s*[?.!]*\s*$",
    re.I,
)


def guess_minutes(title: str) -> int:
    for pattern, minutes in _GUESSES:
        if pattern.search(title or ""):
            return minutes
    return DEFAULT_MINUTES


def parse_duration(text: str) -> int | None:
    """'2h', '90m', '1.5 hours', '1h 30m', '45' (minutes) -> minutes."""
    text = text.strip().lower()
    if text.isdigit():
        return int(text) if 0 < int(text) <= 1440 else None
    found = _DURATION.search(text)
    if not found or not (found["h"] or found["m"]):
        return None
    minutes = round(float(found["h"] or 0) * 60) + int(found["m"] or 0)
    return minutes if 0 < minutes <= 1440 else None


def _hhmm(raw: str, fallback: time) -> time:
    try:
        hour, minute = (int(x) for x in raw.split(":"))
        return time(hour, minute)
    except (ValueError, AttributeError):
        return fallback


def clock(moment: datetime) -> str:
    hour = moment.hour % 12 or 12
    return f"{hour}:{moment.minute:02d} {'AM' if moment.hour < 12 else 'PM'}"


def span(minutes: int) -> str:
    hours, mins = divmod(max(0, minutes), 60)
    if hours and mins:
        return f"{hours}h {mins:02d}m"
    return f"{hours}h" if hours else f"{mins}m"


@dataclass(frozen=True)
class Block:
    start: datetime
    end: datetime

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)


@dataclass(frozen=True)
class Slot:
    start: datetime
    end: datetime
    title: str
    due: str
    part: str = ""  # "(part 1)" when a task is split across stretches


@dataclass(frozen=True)
class Plan:
    day: date
    free: list[Block]
    slots: list[Slot]
    unplaced: list[tuple[str, str, int]]  # (title, due, minutes left)
    work_minutes: int

    @property
    def free_minutes(self) -> int:
        return sum(b.minutes for b in self.free)


def free_blocks(day: date, *, now: datetime, busy: list[tuple[datetime, datetime]], start: time,
                end: time, zone: ZoneInfo) -> list[Block]:
    """The day's free time: [start, end] minus busy, and nothing before now."""
    begin = datetime.combine(day, start, tzinfo=zone)
    finish = datetime.combine(day, end, tzinfo=zone)
    if now > begin:
        # Round up to the next five minutes: "start at 7:03" is not a plan.
        rounded = now.astimezone(zone).replace(second=0, microsecond=0)
        rounded += timedelta(minutes=(-rounded.minute) % 5)
        begin = max(begin, rounded)
    blocks = [Block(begin, finish)] if finish > begin else []
    # Rows come back from the database in UTC; every edge is his local time.
    for b_start, b_end in sorted((s.astimezone(zone), e.astimezone(zone)) for s, e in busy):
        next_blocks = []
        for block in blocks:
            if b_end <= block.start or b_start >= block.end:
                next_blocks.append(block)
                continue
            if b_start > block.start:
                next_blocks.append(Block(block.start, b_start))
            if b_end < block.end:
                next_blocks.append(Block(b_end, block.end))
        blocks = next_blocks
    return [b for b in blocks if b.minutes >= MIN_CHUNK]


def fill(blocks: list[Block], tasks: list[tuple[str, str, int]]) -> tuple[list[Slot], list[tuple[str, str, int]]]:
    """Place (title, due, minutes) tasks in order into the blocks."""
    slots: list[Slot] = []
    unplaced: list[tuple[str, str, int]] = []
    cursor = [(b.start, b.end) for b in blocks]
    index = 0
    for title, due, minutes in tasks:
        left = minutes
        pieces: list[Slot] = []
        while left > 0 and index < len(cursor):
            start, end = cursor[index]
            room = int((end - start).total_seconds() // 60)
            if room < MIN_CHUNK:
                index += 1
                continue
            take = min(left, room)
            pieces.append(Slot(start, start + timedelta(minutes=take), title, due))
            left -= take
            start = start + timedelta(minutes=take + BREAK)
            cursor[index] = (start, end)
            if start >= end:
                index += 1
        if len(pieces) > 1:
            pieces = [Slot(p.start, p.end, p.title, p.due, f"(part {i})") for i, p in enumerate(pieces, 1)]
        slots.extend(pieces)
        if left > 0:
            unplaced.append((title, due, left))
    return slots, unplaced


class Planner(Skill):
    name = "plan"
    help = (
        "`/plan` · `/plan tomorrow` — tonight's free time filled with what's due (no AI)",
        "`/estimate lab writeup 2h` — how long one will take",
    )
    commands = frozenset({"plan", "estimate"})

    def _zone(self) -> ZoneInfo:
        return ZoneInfo(self.ctx.config.timezone)

    def _due_words(self, row: dict, today: date) -> str:
        zone = self._zone()
        due = row["due_at"].astimezone(zone)
        days = (due.date() - today).days
        if days < 0:
            return "overdue"
        at = "" if row.get("all_day") else f" {clock(due)}"
        if days == 0:
            return f"due today{at}"
        if days == 1:
            return f"due tomorrow{at}"
        return f"due {due:%a}{at}"

    async def build_plan(self, day: date) -> Plan:
        zone = self._zone()
        config = self.ctx.config
        now = self.ctx.now()
        today = now.date()
        commute = timedelta(minutes=config.plan_commute_minutes)
        shifts = await self.ctx.store.shifts_between(day, day)
        events = await self.ctx.store.events_between(day, day)
        busy = [(s["starts_at"] - commute, s["ends_at"] + commute) for s in shifts if not s.get("cancelled")]
        busy += [(e["starts_at"], e["ends_at"] or e["starts_at"] + timedelta(hours=1))
                 for e in events if not e.get("all_day")]
        start = _hhmm(config.plan_weekend_start if day.weekday() >= 5 else config.plan_school_day_start, time(15))
        end = _hhmm(config.plan_bedtime, time(22, 30))
        blocks = free_blocks(day, now=now, busy=busy, start=start, end=end, zone=zone)

        rows = await self.ctx.store.plannable_assignments(today + timedelta(days=HORIZON_DAYS))
        # Work due before this plan's free time begins can't wait for it (tomorrow
        # 8 AM is tonight's job, not tomorrow's), except what is already overdue.
        threshold = now if day == today else datetime.combine(day, start, tzinfo=zone)
        rows = [r for r in rows if r["due_at"] >= threshold or r["due_at"] < now]
        rows.sort(key=lambda r: (r["due_at"].astimezone(zone) >= now, r["due_at"]))
        tasks = []
        for r in rows:
            label = safe_field(r["title"], limit=80) + (f" [{safe_field(r['course'], limit=40)}]" if r.get("course") else "")
            minutes = int(r.get("estimate_minutes") or guess_minutes(r["title"]))
            tasks.append((label, self._due_words(r, today), minutes))
        slots, unplaced = fill(blocks, tasks)
        return Plan(day, blocks, slots, unplaced, sum(t[2] for t in tasks))

    def render(self, plan: Plan, label: str) -> Answer:
        if not plan.work_minutes:
            return Answer(f"Nothing due in the next {HORIZON_DAYS} days, so {label} is yours.")
        if not plan.free:
            return Answer(f"You don't have free time left {label}.",
                          f"{span(plan.work_minutes)} of work is due in the next {HORIZON_DAYS} days.")
        lines = [f"{clock(s.start)}–{clock(s.end)}  {s.title} {s.part}".rstrip() + f" — {s.due}" for s in plan.slots]
        if plan.unplaced:
            lines.append("")
            lines.append("Won't fit:")
            lines += [f"• {t} — {d} (about {span(m)} left)" for t, d, m in plan.unplaced]
        first = plan.slots[0] if plan.slots else None
        speech = f"{span(plan.free_minutes)} free {label} and {span(plan.work_minutes)} of work."
        if first is not None:
            speech += f" Start with {first.title.split(' [')[0]} at {clock(first.start)}."
        if plan.unplaced:
            speech = speech.rstrip(".") + f"; {len(plan.unplaced)} won't fit."
        return Answer(speech, "\n".join(lines))

    async def plan(self, when: str) -> Answer:
        today = self.ctx.today()
        if when.strip().lower().startswith("tom"):
            return self.render(await self.build_plan(today + timedelta(days=1)), "tomorrow")
        return self.render(await self.build_plan(today), "tonight" if self.ctx.now().hour >= 12 else "today")

    async def estimate(self, rest: str) -> Answer:
        words = rest.strip().split()
        if len(words) < 2:
            return Answer("Try /estimate lab writeup 2h.")
        minutes = None
        for k in (2, 1):
            if len(words) > k:
                minutes = parse_duration(" ".join(words[-k:]))
                if minutes:
                    words = words[:-k]
                    break
        if not minutes:
            return Answer("How long? Try 45m, 2h or 1h 30m.")
        wanted = set(re.findall(r"\w+", " ".join(words).lower()))
        rows = await self.ctx.store.outstanding_assignments()
        hits = [r for r in rows
                if wanted <= set(re.findall(r"\w+", f"{r['title']} {r.get('course') or ''}".lower()))]
        if len(hits) != 1:
            if not hits:
                return Answer("I don't see an open assignment like that.")
            return Answer(f"{len(hits)} match; say a bit more.", "\n".join(f"• {r['title']}" for r in hits[:10]))
        row = await self.ctx.store.set_estimate(str(hits[0]["id"]), minutes)
        return Answer(f"Got it: {row['title']} takes about {span(minutes)}.")

    async def command(self, name: str, rest: str) -> Answer | None:
        if name == "estimate":
            return await self.estimate(rest)
        return await self.plan(rest)

    async def match(self, text: str) -> Answer | None:
        found = _ASK.match(text.replace("’", "'"))
        if found is None:
            return None
        return await self.plan(found["when"] or found["when2"] or "")

    async def facts(self) -> list[str]:
        now = self.ctx.now()
        plan = await self.build_plan(now.date())
        if not plan.work_minutes:
            return []
        label = "tonight" if now.hour >= 12 else "today"
        start = f" from {clock(plan.free[0].start)}" if plan.free else ""
        first = f"; first: {plan.slots[0].title}" if plan.slots else ""
        return [f"- PLAN {label}: {span(plan.free_minutes)} free{start}; {span(plan.work_minutes)} of work due "
                f"in the next {HORIZON_DAYS} days{first}"
                + (f"; {len(plan.unplaced)} won't fit" if plan.unplaced else "")]

    async def panel(self) -> dict | None:
        now = self.ctx.now()
        plan = await self.build_plan(now.date())
        if not plan.slots:
            return None
        return {"title": "Tonight" if now.hour >= 12 else "Today's plan",
                "lines": [f"{clock(s.start)} {s.title.split(' [')[0]}" for s in plan.slots[:8]]}


def build(ctx: SkillContext) -> Skill:
    return Planner(ctx)
