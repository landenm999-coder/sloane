"""The five daily jobs.

    06:35  morning_brief   the day ahead            every day
    14:45  pre_shift       before leaving for work  Mon-Fri
    19:05  post_shift      what is left tonight     Mon-Fri
    22:00  wrap            what slipped, tomorrow   every day
    00:15  reflection      rebuild tier 2, prune    every day, SILENT

Plus two that feed them: `entity_sync` (Canvas, calendar, shifts; silent) and
`inbox` (Gmail triage every three hours, 7 AM-7 PM; speaks only when something
needs him).

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
from typing import TYPE_CHECKING
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sloane.agent import Agent
from sloane.config import Settings
from sloane.contract import Reply
from sloane.jobs.governor import Governor
from sloane.memory.store import Store, remember

if TYPE_CHECKING:
    from sloane.mail.inbox import Inbox

log = logging.getLogger(__name__)

Sender = Callable[[Reply], Awaitable[None]]


@dataclass
class JobResult:
    name: str
    ran: bool
    sent: bool = False
    reason: str = ""
    reply: Reply | None = None
    # Ran, but not everything worked (e.g. Canvas down while the calendar
    # synced). Recorded as "partial" so the watchdog can see it.
    partial: bool = False


@dataclass
class JobContext:
    store: Store
    agent: Agent
    governor: Governor
    config: Settings
    send: Sender | None = None
    # P4. None until Gmail is configured; the inbox job says so and stops.
    inbox: Inbox | None = None
    # Plain text to Landen's chat (the bot builds the Reply). For messages that
    # are not an agent turn: a reminder is his own words, not something to ask
    # a model about.
    say: Callable[[str], Awaitable[None]] | None = None
    # A brief as a voice note (text if voice fails). Used for VOICE_BRIEFS.
    speak: Sender | None = None
    # A reminder with snooze buttons: (text, reminder id). Falls back to `say`.
    remind: Callable[[str, str], Awaitable[None]] | None = None

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
    deliver = ctx.speak if (ctx.speak is not None and name in ctx.config.voice_brief_names) else ctx.send
    if deliver is not None:
        try:
            await deliver(reply)
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
    """Canvas, the calendar and shifts; then any Canvas changes, in one message.

    The sync itself is silent and runs at any hour. The change alert is held
    through quiet hours and goes out with the first sync after 6:30.
    """
    from sloane.school.changes import announce
    from sloane.school.sync import sync_all

    report = await sync_all(ctx.store, ctx.config)
    reason = report.speech()
    sent = False
    if ctx.say is not None and ctx.governor.may_send(now):
        try:
            told = await announce(ctx.store, ctx.say)
        except Exception as exc:  # noqa: BLE001 - the alert waits for the next run
            reason += f"; change alert not delivered: {exc}"
        else:
            sent = told > 0
            if told:
                reason += f"; announced {told} Canvas change{'s' if told != 1 else ''}"
    return JobResult("entity_sync", ran=True, sent=sent, reason=reason, partial=not report.ok)


INBOX_QUESTION = (
    "New email was just triaged; it is under INGESTED. In one or two spoken "
    "sentences, tell me what needs me and how urgently. In detail, one line per "
    "email that needs me, plus any reply you drafted and are waiting on my OK "
    "for. Don't list the fyi or ignored ones."
)


async def inbox(ctx: JobContext, now: datetime | None = None) -> JobResult:
    """Triage new mail, draft replies, and tell him only if something needs him."""
    from sloane.mail import MailError
    from sloane.mail.inbox import TriageError
    from sloane.router import NoProviderAvailable

    if ctx.inbox is None:
        return JobResult("inbox", ran=False, reason="gmail is not configured")
    # It may message him (drafts come with buttons), so it respects quiet hours,
    # and triage is a bulk call.
    decision = await ctx.governor.may_run(sends_message=True, purpose="bulk", now=now)
    if not decision:
        return JobResult("inbox", ran=False, reason=decision.reason)
    drafting = await ctx.governor.may_spend("reply")

    try:
        report = await ctx.inbox.run(may_draft=bool(drafting))
    except (MailError, NoProviderAvailable, TriageError) as exc:
        return JobResult("inbox", ran=False, reason=f"inbox not triaged: {exc}")

    c = report.counts()
    summary = (
        f"{report.checked} unread checked, {len(report.triaged)} new: "
        f"{c['urgent']} urgent, {c['reply']} reply, {c['fyi']} fyi, {c['ignore']} ignore; "
        f"{len(report.proposed)} drafted"
        + (f"; {'; '.join(report.notes)}" if report.notes else "")
    )
    # Quiet unless something needs him. Four checks a day that each say
    # "nothing important" would train him to ignore the fifth.
    if not report.needs_him or ctx.send is None:
        return JobResult("inbox", ran=True, sent=False, reason=summary)

    reply = await ctx.agent.answer(
        INBOX_QUESTION, channel="job:inbox", today=ctx.today(), ingested=report.ingested()
    )
    try:
        await ctx.send(reply)
    except Exception as exc:  # noqa: BLE001 - recorded, not raised
        return JobResult("inbox", ran=True, sent=False, reason=f"{summary}; delivery failed: {exc}",
                         reply=reply)
    return JobResult("inbox", ran=True, sent=True, reason=summary, reply=reply)


# A reminder this late says when it was for, so "call Keegan" at 6:30 AM is
# not mistaken for something he meant for 6:30 AM.
LATE_AFTER = timedelta(minutes=15)


async def reminders(ctx: JobContext, now: datetime | None = None) -> JobResult:
    """Deliver due reminders. No model call; quiet hours hold them till morning."""
    from sloane.reminders import spoken

    if ctx.say is None:
        return JobResult("reminders", ran=False, reason="no chat to deliver to")
    speaking = ctx.governor.may_send(now)
    if not speaking:
        return JobResult("reminders", ran=False, reason=speaking.reason)

    zone = ZoneInfo(ctx.config.timezone)
    moment = (now or datetime.now(zone)).astimezone(zone)
    # UPDATE ... RETURNING has no order; deliver in the order they were due.
    due = sorted(await ctx.store.claim_due_reminders(moment), key=lambda r: r["due_at"])
    delivered = 0
    for row in due:
        text = f"⏰ {row['text']}"
        due_at = row["due_at"].astimezone(zone)
        if moment - due_at > LATE_AFTER:
            text += f" (this was for {spoken(due_at, moment).removeprefix('at ')})"
        try:
            if ctx.remind is not None:
                await ctx.remind(text, str(row["id"]))
            else:
                await ctx.say(text)
            delivered += 1
        except Exception as exc:  # noqa: BLE001 - unclaim and retry next tick
            log.warning("reminder %s not delivered, will retry: %s", row["id"], exc)
            await remember("unclaim reminder", ctx.store.unclaim_reminder(str(row["id"])))
    return JobResult(
        "reminders", ran=True, sent=delivered > 0,
        reason=f"{delivered} delivered" + (f", {len(due) - delivered} retrying" if delivered < len(due) else ""),
    )


async def watchdog(ctx: JobContext, now: datetime | None = None) -> JobResult:
    """Every 30 minutes: is anything broken? Told once, repeated daily, cleared when fixed."""
    from sloane.jobs import watchdog as wd

    if ctx.say is None:
        return JobResult("watchdog", ran=False, reason="no chat to deliver to")
    speaking = ctx.governor.may_send(now)
    if not speaking:
        return JobResult("watchdog", ran=False, reason=speaking.reason)
    zone = ZoneInfo(ctx.config.timezone)
    moment = (now or datetime.now(zone)).astimezone(zone)
    present, sent = await wd.run(ctx.store, ctx.config, ctx.say, moment)
    return JobResult("watchdog", ran=True, sent=sent > 0,
                     reason=f"{present} problem{'s' if present != 1 else ''} present")


WEEKLY_QUESTION = (
    "It's Sunday evening: give me my weekly review. First the week behind -- "
    "use the record below for what got graded or went missing and which "
    "promises I kept -- then the week ahead from FACTS: what's due, which days "
    "are heavy around my shifts, any conflicts, and open promises. End with the "
    "one thing to start tonight. Keep the spoken part to two sentences."
)


async def weekly_review(ctx: JobContext, now: datetime | None = None) -> JobResult:
    """Sunday 7 PM: the week behind (from our own records), the week ahead (FACTS)."""
    from sloane.ingest import safe_field

    decision = await ctx.governor.may_run(sends_message=True, now=now)
    if not decision:
        return JobResult("weekly_review", ran=False, reason=decision.reason)
    zone = ZoneInfo(ctx.config.timezone)
    since = (now or datetime.now(zone)).astimezone(zone) - timedelta(days=7)
    changes = await ctx.store.school_changes_since(since)
    kept = await ctx.store.commitments_closed_since(since)
    record = [
        f"- {c['kind'].upper()}: {safe_field(c['title'], limit=120)}"
        + (f" [{safe_field(c['course'], limit=60)}]" if c.get("course") else "")
        + (f" {safe_field(c['detail'], limit=40)}" if c.get("detail") else "")
        for c in changes if c["kind"] in ("graded", "missing")
    ] + [f"- PROMISE {k['status'].upper()}: {safe_field(k['what'], limit=120)}" for k in kept]
    question = WEEKLY_QUESTION + "\n\nThis week's record:\n" + ("\n".join(record) or "- (nothing recorded)")
    reply = await ctx.agent.answer(question, channel="job:weekly_review", today=ctx.today())
    deliver = ctx.speak if (ctx.speak and "weekly_review" in ctx.config.voice_brief_names) else ctx.send
    if deliver is None:
        return JobResult("weekly_review", ran=True, sent=False, reply=reply)
    try:
        await deliver(reply)
    except Exception as exc:  # noqa: BLE001
        return JobResult("weekly_review", ran=True, sent=False, reason=f"delivery failed: {exc}", reply=reply)
    return JobResult("weekly_review", ran=True, sent=True, reply=reply)


async def backup(ctx: JobContext, now: datetime | None = None) -> JobResult:
    """Nightly, silent: the data only Landen could recreate, as JSON, 14 kept."""
    import json
    from pathlib import Path

    folder = ctx.config.backup_dir or (
        str(Path(ctx.config.embed_cache_dir) / "backups") if ctx.config.embed_cache_dir else ""
    )
    if not folder:
        return JobResult("backup", ran=False, reason="no BACKUP_DIR or EMBED_CACHE_DIR to write to")
    zone = ZoneInfo(ctx.config.timezone)
    stamp = (now or datetime.now(zone)).astimezone(zone)
    data = await ctx.store.export()
    target = Path(folder)
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"sloane-{stamp:%Y-%m-%d}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"exported_at": stamp.isoformat(), "tables": data}, default=str, indent=1))
    tmp.replace(path)  # atomic: a crash mid-write never leaves a torn backup
    old = sorted(target.glob("sloane-*.json"))[: -max(1, ctx.config.backup_keep)]
    for stale in old:
        stale.unlink(missing_ok=True)
    rows = sum(len(v) for v in data.values())
    return JobResult("backup", ran=True, sent=False, reason=f"{rows} rows -> {path.name}")


HANDLERS: dict[str, Callable[..., Awaitable[JobResult]]] = {
    "morning_brief": morning_brief,
    "pre_shift": pre_shift,
    "post_shift": post_shift,
    "wrap": wrap,
    "reflection": reflection,
    "entity_sync": entity_sync,
    "inbox": inbox,
    "reminders": reminders,
    "watchdog": watchdog,
    "weekly_review": weekly_review,
    "backup": backup,
}
