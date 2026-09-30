"""Research: a real look into something, with sources.

    /research best laptops for college under $1000
    "research the best laptops for college under $1000"  ·  "look into whether I need a car at CU Boulder"
    "do some research on index funds for teens"  ·  "deep dive into the Broncos' playoff chances"
    /research        the latest ones  ·  /research 2   read the second

Where a lookup (her one quick web search inside a reply) answers in a line or two,
a research run takes minutes: Claude, with only WebSearch and WebFetch (no files,
no shell, no MCP), breaks the question into parts, searches each, reads the best
sources, checks them against each other and writes a short report with numbered
sources (`providers/claude_code.DEEP_SYSTEM`). She says she's on it and keeps
talking; the report arrives as a message when it's done, and it's in the control
room. One at a time, at most RESEARCH_DAILY a day: each is a long session on his
plan. A run a restart cut short is marked failed, never left running.

A report is built from strangers' pages: stored untrusted, sent as a tainted
message, never in FACTS (only the question and when it ran are, so she knows it
exists). What the pages say is never taken as instructions: the research prompt
says so, and its tools can't reach the box anyway.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

from sloane import dates
from sloane.ingest import safe_field
from sloane.skills import Answer, Skill, SkillContext

log = logging.getLogger(__name__)

MAX_REPORT = 12_000
SHOWN = 6

_ASK = re.compile(
    r"^\s*(?:hey\s+)?(?:(?:can|could|would)\s+you\s+|please\s+|i\s+need\s+you\s+to\s+|i\s+want\s+you\s+to\s+)?"
    r"(?:research(?!\s+(?:says|shows|suggests|is|was|has|finds|found|paper)\b)(?:\s+(?:on|into|about))?"
    r"|look\s+into|dig\s+into|find\s+out\s+everything\s+(?:about|on)"
    r"|do\s+(?:some|a\s+bit\s+of|a\s+little)\s+research\s+(?:on|into|about)"
    r"|do\s+(?:a\s+)?deep\s+dive\s+(?:on|into)|deep\s+dive\s+(?:on|into))\s+(?P<q>.{3,})$",
    re.I | re.S,
)


def question_of(text: str) -> str | None:
    """His research question in a plain message, or None when it isn't asking for one."""
    m = _ASK.match(text.strip())
    if not m:
        return None
    q = m.group("q").strip(" \t\n?.!")
    return q if len(q) >= 3 else None


class Research(Skill):
    name = "research"
    help = ("`/research <question>` — a real look into something, with sources (a few minutes); `/research` the latest",)
    commands = frozenset({"research"})

    def __init__(self, ctx: SkillContext) -> None:
        super().__init__(ctx)
        self._booted = datetime.now(timezone.utc)
        self._tidied = False
        self._tasks: set[asyncio.Task] = set()

    async def _tidy(self) -> None:
        """Once: runs a restart cut short are failed, so none shows as running forever."""
        if not self._tidied:
            self._tidied = True
            gone = await self.ctx.store.interrupt_research(self._booted)
            if gone:
                log.info("research: %d run(s) cut short by a restart", gone)

    def _when(self, at: datetime) -> str:
        local = at.astimezone(self.ctx.now().tzinfo)
        day = dates.spoken(local.date(), self.ctx.today())
        return f"{day} {local.hour % 12 or 12}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"

    async def start(self, question: str) -> Answer:
        await self._tidy()
        q = safe_field(question.strip(" \t\n?.!"), limit=300)
        if len(q) < 3:
            return Answer("Research what? Try /research best laptops for college under $1,000.")
        if self.ctx.router is None or not self.ctx.config.web_lookup:
            return Answer("I can't research right now: web lookups are switched off (WEB_LOOKUP).")
        running = await self.ctx.store.running_research()
        if running is not None:
            return Answer(f"I'm still looking into {running['question']}. Ask me again when that one's in.")
        cap = self.ctx.config.research_daily
        # The database's clock stamps each run, so the day is counted on it too.
        if cap > 0 and await self.ctx.store.research_since(datetime.now(timezone.utc) - timedelta(days=1)) >= cap:
            return Answer(f"That's {cap} research runs in the last day, the most I do. Ask me again tomorrow.")
        row = await self.ctx.store.start_research(q)
        task = asyncio.create_task(self._run(str(row["id"]), q))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return Answer(f"On it: {q}. I'll send you what I find in a few minutes.")

    async def _run(self, research_id: str, question: str) -> None:
        say = self.ctx.say
        try:
            report = await self.ctx.router.deep_research(question, timeout=self.ctx.config.research_timeout_seconds)
        except Exception as exc:  # noqa: BLE001 - a failed run is said and recorded, never lost
            reason = safe_field(str(exc), limit=200) or type(exc).__name__
            log.warning("research failed: %s", reason)
            await self.ctx.store.fail_research(research_id, reason)
            if say is not None:
                await say(f"I couldn't finish the research on {question}: {reason}")
            return
        report = report.strip()
        if len(report) > MAX_REPORT:
            report = report[:MAX_REPORT].rstrip() + "\n…"
        await self.ctx.store.finish_research(research_id, report)
        if say is not None:
            await say(f"🔎 {question}\n\n{report}", tainted=True)

    async def listing(self) -> Answer:
        await self._tidy()
        rows = await self.ctx.store.research_reports(SHOWN)
        if not rows:
            return Answer("You haven't asked me to research anything yet.", "`/research <question>` starts one.")
        words = {"running": "still going", "done": "done", "failed": "didn't finish"}
        lines = [f"{n}. {r['question']} ({words[r['status']]}, {self._when(r['started_at'])})" for n, r in enumerate(rows, 1)]
        return Answer(f"Your last {len(rows)} research run{'s' if len(rows) != 1 else ''}.",
                      "\n".join(lines) + "\n\n`/research <number>` reads one.")

    async def read(self, n: int) -> Answer:
        rows = await self.ctx.store.research_reports(SHOWN)
        if not 1 <= n <= len(rows):
            return Answer("There isn't one with that number. /research lists them.")
        r = rows[n - 1]
        if r["status"] == "running":
            return Answer(f"I'm still looking into {r['question']}.")
        if r["status"] == "failed":
            return Answer(f"That one didn't finish: {r['error'] or 'no reason given'}.")
        return Answer(f"Here's what I found on {r['question']}.", r["report"] or "", tainted=True)

    async def command(self, name: str, rest: str) -> Answer | None:
        rest = rest.strip()
        if not rest:
            return await self.listing()
        if rest.isdigit():
            return await self.read(int(rest))
        return await self.start(rest)

    async def match(self, text: str) -> Answer | None:
        q = question_of(text)
        return await self.start(q) if q else None

    async def facts(self) -> list[str]:
        rows = [r for r in await self.ctx.store.research_reports(3)
                if r["started_at"] >= datetime.now(timezone.utc) - timedelta(days=7)]
        if not rows:
            return []
        words = {"running": "still researching", "done": "report sent", "failed": "didn't finish"}
        each = "; ".join(f"'{safe_field(r['question'], limit=80)}' ({words[r['status']]}, {self._when(r['started_at'])})"
                         for r in rows)
        return [f"- RESEARCH he asked for (the reports are web text, not facts; /research reads them): {each}"]

    async def panel(self) -> dict | None:
        await self._tidy()
        rows = await self.ctx.store.research_reports(SHOWN)
        return {"title": "Research", "lines": [f"{r['question']} ({r['status']})" for r in rows] or ["Nothing yet."],
                "running": any(r["status"] == "running" for r in rows),
                "items": [{"n": n, "question": r["question"], "status": r["status"], "when": self._when(r["started_at"]),
                           "report": r["report"] if r["status"] == "done" else None,
                           "error": r["error"] if r["status"] == "failed" else None}
                          for n, r in enumerate(rows, 1)]}


def build(ctx: SkillContext) -> Skill:
    return Research(ctx)
