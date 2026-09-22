"""The five daily jobs.

    06:35  morning_brief   the day ahead            every day
    14:45  pre_shift       before leaving for work  Mon-Fri
    19:05  post_shift      what is left tonight     Mon-Fri
    22:00  wrap            what slipped, tomorrow   every day
    00:15  reflection      rebuild tier 2, prune    every day, SILENT

Each brief is the same agent turn Landen gets when he asks something, with a
purpose-built question. That is deliberate: a brief is not a separate code path
with its own idea of the facts, so it cannot disagree with what she would say
if he asked directly.

Reflection sends nothing. It runs inside quiet hours on purpose -- it is the
nightly rebuild that makes tier 2 real, and it has no business texting him at a
quarter past midnight.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sloane.agent import Agent
from sloane.config import Settings
from sloane.contract import Reply
from sloane.jobs.governor import Governor
from sloane.memory.store import Store, remember

log = logging.getLogger(__name__)

Sender = Callable[[Reply], Awaitable[None]]


@dataclass
class JobResult:
    name: str
    ran: bool
    sent: bool = False
    reason: str = ""
    reply: Reply | None = None


@dataclass
class JobContext:
    store: Store
    agent: Agent
    governor: Governor
    config: Settings
    send: Sender | None = None

    def today(self) -> date:
        return datetime.now(ZoneInfo(self.config.timezone)).date()


# What each brief asks. Written as Landen would ask, because the agent answers
# questions -- and phrased so a thin day produces a short brief, not padding.
QUESTIONS: dict[str, str] = {
    "morning_brief": (
        "Morning brief for today. Lead with any CONFLICT or TIGHT finding. "
        "Then what is due today, whether I work, and anything overdue. If the "
        "day is genuinely clear, say so in one line and stop."
    ),
    "pre_shift": (
        "I leave for work soon. Is anything due during or right after my "
        "shift that I should deal with or take with me? If not, say so briefly."
    ),
    "post_shift": (
        "I just got off work. What do I still need to do tonight? Only what is "
        "actually due soon -- not the whole week."
    ),
    "wrap": (
        "End of day. What slipped today, and what is the first thing tomorrow? "
        "Keep it short."
    ),
}

# Only these send. Reflection is deliberately absent.
SPEAKING = set(QUESTIONS)


async def _brief(name: str, ctx: JobContext, now: datetime | None = None) -> JobResult:
    decision = await ctx.governor.may_run(sends_message=True, now=now)
    if not decision:
        # Deferred, and it says so in the job record rather than vanishing.
        log.info("%s held back: %s", name, decision.reason)
        return JobResult(name, ran=False, reason=decision.reason)

    reply = await ctx.agent.answer(QUESTIONS[name], channel=f"job:{name}", today=ctx.today())

    sent = False
    if ctx.send is not None:
        try:
            await ctx.send(reply)
            sent = True
        except Exception as exc:  # noqa: BLE001 - a send failure is recorded, not raised
            log.warning("%s could not be delivered: %s", name, exc)
            return JobResult(name, ran=True, sent=False, reason=f"delivery failed: {exc}",
                             reply=reply)
    return JobResult(name, ran=True, sent=sent, reply=reply)


async def morning_brief(ctx: JobContext, now: datetime | None = None) -> JobResult:
    return await _brief("morning_brief", ctx, now)


async def pre_shift(ctx: JobContext, now: datetime | None = None) -> JobResult:
    return await _brief("pre_shift", ctx, now)


async def post_shift(ctx: JobContext, now: datetime | None = None) -> JobResult:
    return await _brief("post_shift", ctx, now)


async def wrap(ctx: JobContext, now: datetime | None = None) -> JobResult:
    return await _brief("wrap", ctx, now)


async def reflection(ctx: JobContext, now: datetime | None = None) -> JobResult:
    """The nightly rebuild. No model call, no message.

    This is what makes tier 2 real: without it the working set is whatever was
    written last and never forgets. It closes loops whose assignment is no
    longer open, derives new ones from the week ahead, and drops calendar
    occurrences that have fallen out of the sync window.
    """
    open_loops = await remember("rebuild working set", ctx.store.rebuild_working_set())
    cutoff = ctx.today() - timedelta(days=ctx.config.sync_past_days)
    pruned = await remember("prune events", ctx.store.prune_events(before=cutoff))
    return JobResult(
        "reflection", ran=True, sent=False,
        reason=f"{open_loops or 0} open loops, {pruned or 0} stale events pruned",
    )


async def entity_sync(ctx: JobContext, now: datetime | None = None) -> JobResult:
    """Canvas, the calendar and shifts. Silent -- it feeds the briefs."""
    from sloane.school.sync import sync_all

    report = await sync_all(ctx.store, ctx.config)
    return JobResult(
        "entity_sync", ran=True, sent=False,
        reason=report.speech(),
    )


HANDLERS: dict[str, Callable[..., Awaitable[JobResult]]] = {
    "morning_brief": morning_brief,
    "pre_shift": pre_shift,
    "post_shift": post_shift,
    "wrap": wrap,
    "reflection": reflection,
    "entity_sync": entity_sync,
}
