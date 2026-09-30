"""The five daily jobs.

    06:35  morning_brief   the day ahead            every day
    14:45  pre_shift       before leaving for work  Mon-Fri
    19:05  post_shift      what is left tonight     Mon-Fri
    22:00  wrap            what slipped, tomorrow   every day
    00:15  reflection      rebuild tier 2, prune    every day, SILENT
    10:25, 12:25, 16:25, 20:25  think   one thing worth saying, or nothing
                                        (not in class, not on a shift)
    01:10  workshop        the night shift: build what he queued, then an
                           idea of her own; all of it waits for his Accept

Plus two that feed them: `entity_sync` (Canvas, calendar, shifts; silent) and
`inbox` (Gmail triage every three hours, 7 AM-7 PM; speaks only when something
needs him). And `heartbeat`, every quarter hour from 7 AM to 10 PM, which asks
each skill (sloane/skills/) whether anything is worth saying now -- rain before
his shift, a streak about to break -- and says each thing once, with no model.

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
from typing import TYPE_CHECKING, Any
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sloane.agent import Agent
from sloane.config import Settings
from sloane.contract import Reply
from sloane.ingest import planted, safe_field
from sloane.jobs.governor import Governor
from sloane.memory.store import Store, remember

if TYPE_CHECKING:
    from sloane.mail.inbox import Inbox
    from sloane.skills import Registry

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
    # Every loaded skill (sloane.skills.Registry), for jobs that ask them things.
    skills: Registry | None = None
    # The workshop (sloane/workshop.py), for the night shift.
    workshop: Any = None

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

# She thinks: nobody asked, so she speaks only when there's one thing worth it.
THINK_QUESTION = (
    "(Nobody sent a message: this is you, thinking, between conversations.) Look over everything you "
    "know right now -- FACTS, LOOPS, STATE, RECALL and CONVERSATION -- and decide whether there is ONE "
    "thing genuinely worth saying to Landen now that he hasn't already heard from you today: a clash "
    "coming up, a deadline he'll miss at this rate given his free time, something he said he'd do that's "
    "due, a better plan for tonight, how something he was worried about went, or an idea for the "
    "business or DECA that fits today. Say it the way a friend who noticed would, in a line or two. If "
    "you'd offer to do something, offer it (\"Want me to remind you at 6?\"): his yes is what makes it "
    "happen. If nothing clears that bar, or CONVERSATION shows you already said it, make speech exactly "
    "NOTHING. Most of the time, NOTHING is right."
)
SILENT = "NOTHING"
# Weekday school hours, when a thought can wait for lunch.
SCHOOL = (7 * 60 + 45, 14 * 60 + 30)


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


async def think(ctx: JobContext, now: datetime | None = None) -> JobResult:
    """A few times a day: anything worth saying? Usually not, and then nothing is sent.

    Not in class, not on a shift. Counted against the scheduled-work budget
    like a brief, and a silent turn is not kept as a memory. What she offers
    to do, she only does if he says yes (actions.grounded reads the offer).
    """
    if not ctx.config.think:
        return JobResult("think", ran=False, reason="THINK is off")
    zone = ZoneInfo(ctx.config.timezone)
    moment = (now or datetime.now(zone)).astimezone(zone)
    minute = moment.hour * 60 + moment.minute
    if moment.weekday() < 5 and SCHOOL[0] <= minute < SCHOOL[1]:
        return JobResult("think", ran=False, reason="he's at school")
    try:
        shifts = await ctx.store.shifts_between(moment.date(), moment.date())
    except Exception:  # noqa: BLE001 - unknown is not "at work"
        shifts = []
    if any(s.get("starts_at") and s.get("ends_at") and not s.get("cancelled")
           and s["starts_at"] <= moment < s["ends_at"] for s in shifts):
        return JobResult("think", ran=False, reason="he's at work")
    decision = await ctx.governor.may_run(sends_message=True, now=now)
    if not decision:
        return JobResult("think", ran=False, reason=decision.reason)

    reply = await ctx.agent.answer(THINK_QUESTION, channel="job:think", today=moment.date(), persist=False)
    said = (reply.speech or "").strip().strip(".!").upper()
    if not said or said.startswith(SILENT):
        return JobResult("think", ran=True, sent=False, reason="nothing worth saying")
    if ctx.send is None:
        return JobResult("think", ran=True, sent=False, reason="no chat to deliver to", reply=reply)
    try:
        await ctx.send(reply)
    except Exception as exc:  # noqa: BLE001 - recorded, not raised
        return JobResult("think", ran=True, sent=False, reason=f"delivery failed: {exc}", reply=reply)
    return JobResult("think", ran=True, sent=True, reason="said one thing", reply=reply)


async def workshop(ctx: JobContext, now: datetime | None = None) -> JobResult:
    """1:10 AM, silent: build what he queued, then (with room) one idea of her own.

    Everything built waits in the workshop for his Accept; the heartbeat tells
    him in the morning. Nothing here reaches the running code.
    """
    shop = ctx.workshop
    if shop is None or not ctx.config.workshop_nightly:
        return JobResult("workshop", ran=False, reason="the night shift is off")
    if not shop.ready:
        return JobResult("workshop", ran=False, reason="no GitHub token, so nothing can be built")
    decision = await ctx.governor.may_run(sends_message=False, now=now)
    if not decision:
        return JobResult("workshop", ran=False, reason=decision.reason)
    return JobResult("workshop", ran=True, sent=False, reason=await shop.nightly())


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


def _leads_with_symbol(text: str) -> bool:
    import unicodedata

    return bool(text) and unicodedata.category(text[0]) == "So"


async def _schedule_next(ctx: JobContext, row: dict, moment: datetime, zone: ZoneInfo) -> None:
    from datetime import time as clock

    from sloane.reminders import next_due

    quiet = (clock(ctx.config.quiet_start_hour, 0), clock(ctx.config.quiet_end_hour, ctx.config.quiet_end_minute))
    after = row["due_at"].astimezone(zone)
    nxt = next_due(row["repeat"], after, quiet=quiet)
    for _ in range(1000):  # a rule always moves forward; the cap is belt and braces
        if nxt > moment:
            break
        nxt = next_due(row["repeat"], nxt, quiet=quiet)
    await ctx.store.add_next_reminder(row, nxt)


async def reminders(ctx: JobContext, now: datetime | None = None) -> JobResult:
    """Deliver due reminders. No model call; quiet hours hold them till morning."""
    from sloane.reminders import spoken

    if ctx.say is None:
        return JobResult("reminders", ran=False, reason="no chat to deliver to")
    speaking = ctx.governor.may_send(now)

    zone = ZoneInfo(ctx.config.timezone)
    moment = (now or datetime.now(zone)).astimezone(zone)
    # UPDATE ... RETURNING has no order; deliver in the order they were due.
    # Quiet hours hold everything but a timer: he set that one minutes ago,
    # awake, and a timer that goes off at 6:30 AM is no timer at all.
    due = sorted(await ctx.store.claim_due_reminders(moment, sources=None if speaking else ("timer",)),
                 key=lambda r: r["due_at"])
    if not speaking and not due:
        return JobResult("reminders", ran=False, reason=speaking.reason)
    delivered = 0
    for row in due:
        if row.get("repeat"):
            # The next of the series, made before this one is sent: a send that
            # fails is retried, but the series never silently stops. Several
            # missed (the box was off) come back as one, then the next ahead.
            await remember("next repeating reminder", _schedule_next(ctx, row, moment, zone))
        # A timer or a focus session's end carries its own emoji already.
        text = row["text"] if _leads_with_symbol(row["text"]) else f"⏰ {row['text']}"
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


PLANTED_DAYS = 14  # how far ahead the calendar is checked for planted text


async def planted_nudges(ctx: JobContext, now: datetime | None = None) -> list[tuple[str, str]]:
    """(key, text): a calendar entry written as orders to her, told to him once.

    FACTS marks the entry on every turn so she neither obeys it nor repeats the
    warning; this is the one time he hears about it.
    """
    zone = ZoneInfo(ctx.config.timezone)
    today = (now.astimezone(zone) if now else datetime.now(zone)).date()
    out = []
    for e in await ctx.store.events_between(today, today + timedelta(days=PLANTED_DAYS)):
        if not (planted(e.get("title")) or planted(e.get("location"))):
            continue
        start = e["starts_at"].astimezone(zone)
        when = f"{start:%a %b} {start.day}" + ("" if e.get("all_day") else f" at {start.strftime('%I:%M %p').lstrip('0')}")
        out.append((f"planted:event:{e['id']}",
                    f"⚠️ A calendar entry on {when} reads like instructions aimed at me: "
                    f"\"{safe_field(e['title'], limit=90)}\". I treat it as data and won't act on it. "
                    "If you don't know who put it there, delete it from your calendar."))
    return out


async def heartbeat(ctx: JobContext, now: datetime | None = None) -> JobResult:
    """Every quarter hour, waking hours: what's worth saying, without a model.

    The skills' nudges, and the core's one: a planted calendar entry, told once.
    Each nudge key is said once, however many ticks offer it; a nudge that could
    not be delivered is offered again on the next tick. Several new nudges in
    one tick go out as one message, never a burst. While a skill holds them (he's
    in a focus block) they're asked for but not said: the next tick after says them.
    """
    if ctx.say is None:
        return JobResult("heartbeat", ran=False, reason="no chat to deliver to")
    speaking = ctx.governor.may_send(now)
    if not speaking:
        return JobResult("heartbeat", ran=False, reason=speaking.reason)

    offered: dict[str, str] = {}
    try:
        for key, text in await planted_nudges(ctx, now):
            offered.setdefault(key, text)
    except Exception:  # noqa: BLE001 - the skills' nudges still go out
        log.exception("could not check the calendar for planted text")
    if ctx.skills is not None:
        for nudge in await ctx.skills.nudges():
            offered.setdefault(nudge.key, nudge.text)
    await remember("prune nudges", ctx.store.prune_nudges())
    if not offered:
        return JobResult("heartbeat", ran=True, reason="nothing to say")
    held = await ctx.skills.holding() if ctx.skills is not None else None
    if held:
        return JobResult("heartbeat", ran=True, sent=False, reason=f"{len(offered)} held: {held}")
    fresh = set(await ctx.store.claim_nudges(list(offered)))
    new = [(key, text) for key, text in offered.items() if key in fresh]
    if not new:
        return JobResult("heartbeat", ran=True, reason=f"{len(offered)} offered, all said before")
    try:
        await ctx.say("\n\n".join(text for _, text in new))
    except Exception as exc:  # noqa: BLE001 - unclaim and retry next tick
        log.warning("heartbeat not delivered, will retry: %s", exc)
        await remember("unclaim nudges", ctx.store.unclaim_nudges([key for key, _ in new]))
        return JobResult("heartbeat", ran=True, sent=False, reason=f"not delivered, will retry: {exc}")
    return JobResult("heartbeat", ran=True, sent=True,
                     reason=f"said {len(new)}: " + ", ".join(key for key, _ in new))


async def monitors(ctx: JobContext, now: datetime | None = None) -> JobResult:
    """Every quarter hour in waking hours: what he asked her to watch for (skills/monitors.py).
    Each one that happened is said once and then it's over; one that couldn't be said is
    kept, and said next time. Quiet hours and a focus block hold it all."""
    watcher = ctx.skills.get("monitors") if ctx.skills is not None else None
    if watcher is None:
        return JobResult("monitors", ran=False, reason="no monitors skill")
    if ctx.say is None:
        return JobResult("monitors", ran=False, reason="no chat to deliver to")
    speaking = ctx.governor.may_send(now)
    if not speaking:
        return JobResult("monitors", ran=False, reason=speaking.reason)
    held = await ctx.skills.holding()
    if held:
        return JobResult("monitors", ran=False, reason=f"held: {held}")
    said = 0
    for monitor_id, text, tainted in await watcher.check():
        try:
            await ctx.say(text, tainted=tainted)
        except Exception as exc:  # noqa: BLE001 - kept, and said on the next run
            log.warning("monitor message not delivered, will retry: %s", exc)
            continue
        await watcher.done(monitor_id, text)
        said += 1
    return JobResult("monitors", ran=True, sent=bool(said), reason=f"said {said}" if said else "nothing yet")


async def bank_sync(ctx: JobContext, now: datetime | None = None) -> JobResult:
    """Every three hours in waking hours, silent: his accounts from SimpleFIN (skills/bank.py)."""
    bank = ctx.skills.get("bank") if ctx.skills is not None else None
    if bank is None:
        return JobResult("bank_sync", ran=False, reason="no bank connected")
    ok, what = await bank.sync()
    return JobResult("bank_sync", ran=ok, sent=False, reason=what)


async def learn(ctx: JobContext, now: datetime | None = None) -> JobResult:
    """12:20 AM, silent: follow-ups and facts from the day he just had (one bulk call)."""
    from sloane.memory.learn import learn as learn_day
    from sloane.router import NoProviderAvailable

    decision = await ctx.governor.may_run(sends_message=False, purpose="bulk", now=now)
    if not decision:
        return JobResult("learn", ran=False, reason=decision.reason)
    router = getattr(ctx.agent, "router", None)
    if router is None:
        return JobResult("learn", ran=False, reason="no model router")
    moment = now or datetime.now(ZoneInfo(ctx.config.timezone))
    try:
        added, facts = await learn_day(ctx.store, router, ctx.config, moment,
                                       embedder=getattr(ctx.agent, "embedder", None))
    except NoProviderAvailable as exc:
        return JobResult("learn", ran=False, reason=f"nothing learned, no model: {exc}")
    return JobResult("learn", ran=True, reason=f"{added} follow-up{'s' if added != 1 else ''}, "
                                               f"{facts} fact{'s' if facts != 1 else ''} learned")


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
    "think": think,
    "workshop": workshop,
    "weekly_review": weekly_review,
    "backup": backup,
    "heartbeat": heartbeat,
    "learn": learn,
    "bank_sync": bank_sync,
    "monitors": monitors,
}
