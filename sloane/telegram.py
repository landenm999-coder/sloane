"""Long-polling Telegram bot: text, voice notes, /usage, /state.

Long polling, not webhooks, and that is a deliberate architecture choice: no
public URL, no TLS certificate, no tunnel. The box reaches out to Telegram, so
it can sit behind Oracle's default security list with nothing open inbound.
Voice notes are free in both directions, which is what makes P3 cost nothing.

P3 swaps the sending half for audio. It reads `reply.speech` and nothing else --
which is why the contract caps speech at two sentences with no markdown.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from sloane.agent import Agent
from sloane.config import Settings, settings as default_settings
from sloane.contract import Reply
from sloane.ingest import safe_field
from sloane.reminders import REMIND_ME
from sloane.memory.store import Store, remember
from sloane.providers.base import ProviderError
from sloane.providers.groq import GroqProvider
from sloane.agency import Agency, callback_data, parse_callback
from sloane.voice import Voice

log = logging.getLogger(__name__)

# Telegram's hard cap on one message. Longer replies are split, never cut.
TELEGRAM_LIMIT = 4096


def split_message(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Split at line breaks (then spaces, then anywhere) so each part fits."""
    parts: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = rest.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip("\n ")
    parts.append(rest)
    return [p for p in parts if p] or [""]


API = "https://api.telegram.org"

# Telegram holds the connection open for poll_timeout seconds; the HTTP read
# timeout has to outlast that or every idle poll looks like a failure.
TIMEOUT_MARGIN = 15


class Bot:
    def __init__(
        self,
        store: Store,
        agent: Agent,
        config: Settings | None = None,
        *,
        voice: "Voice | None" = None,
        agency: "Agency | None" = None,
    ) -> None:
        self._config = config or default_settings()
        self._store = store
        self._agent = agent
        self._voice = voice
        self.agency = agency
        # Set by main once the scheduler exists: runs a named job now.
        self.run_job: Callable[[str], Awaitable[Any]] | None = None
        self._stt = GroqProvider(self._config)
        self._token = self._config.telegram_bot_token
        self._owner = self._config.telegram_chat_id
        self._stop = asyncio.Event()

    # -- transport -------------------------------------------------------------

    def _url(self, method: str) -> str:
        return f"{API}/bot{self._token}/{method}"

    async def _call(self, client: httpx.AsyncClient, method: str, **payload) -> dict:
        response = await client.post(self._url(method), json=payload)
        if response.status_code >= 400:
            raise RuntimeError(f"telegram {method} -> {response.status_code}: {response.text[:200]}")
        body = response.json()
        if not body.get("ok"):
            raise RuntimeError(f"telegram {method} -> {body}")
        return body.get("result") or {}

    async def send(self, chat_id: int, reply: Reply) -> None:
        """Send one Reply. Speech first, detail only when it adds something."""
        text = reply.speech or reply.detail or "(no reply)"
        detail = (reply.detail or "").strip()
        if detail and detail != reply.speech.strip():
            text = f"{text}\n\n{detail}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            for part in split_message(text):
                await self._call(client, "sendMessage", chat_id=chat_id, text=part)
        await remember(
            "outbound message",
            self._store.log_message(
                chat_id=chat_id, direction="out", kind="text", body=text[:4000]
            ),
        )

    async def say(self, text: str) -> None:
        """A plain line to Landen's chat. Nothing if there is no owner."""
        if self._owner:
            await self.send(self._owner, Reply(speech=text, detail=""))

    async def ask(self, proposal: dict) -> int | None:
        """Show a proposal with Approve / Edit / Deny. Returns the message id."""
        if not self._owner:
            return None
        pid = str(proposal["id"])
        keyboard = {"inline_keyboard": [[
            {"text": "Approve", "callback_data": callback_data(pid, "a")},
            {"text": "Edit", "callback_data": callback_data(pid, "e")},
            {"text": "Deny", "callback_data": callback_data(pid, "d")},
        ]]}
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                sent = await self._call(
                    client, "sendMessage", chat_id=self._owner,
                    text=f"OK to do this?\n\n{proposal['preview']}"[:4096],
                    reply_markup=keyboard,
                )
        except (httpx.HTTPError, RuntimeError) as exc:
            log.warning("could not ask for approval: %s", exc)
            return None
        return sent.get("message_id")

    async def remind(self, text: str, reminder_id: str) -> None:
        """Deliver a reminder with snooze buttons. Raises if it could not be sent,
        so the reminders job can put it back and retry."""
        from sloane.reminders import SNOOZE_CODES, snooze_data

        if not self._owner:
            return
        keyboard = {"inline_keyboard": [
            [{"text": label, "callback_data": snooze_data(reminder_id, code)}
             for code, label in SNOOZE_CODES.items()],
        ]}
        async with httpx.AsyncClient(timeout=30.0) as client:
            await self._call(client, "sendMessage", chat_id=self._owner, text=text[:4096],
                             reply_markup=keyboard)
        await remember("outbound reminder", self._store.log_message(
            chat_id=self._owner, direction="out", kind="text", body=text[:4000]))

    async def _snooze(self, reminder_id: str, code: str) -> str:
        from sloane.reminders import snoozed_until, spoken

        if code == "ok":
            return "👍"
        original = await self._store.get_reminder(reminder_id)
        if original is None:
            return "I can't find that reminder any more."
        now = self._now()
        due = snoozed_until(code, now)
        await self._store.add_reminder(text=original["text"], due_at=due, source="snooze")
        return f"Snoozed: I'll remind you {spoken(due, now)}."

    async def _handle_callback(self, update: dict) -> None:
        """A button press. Only Landen's chat decides anything.

        With no TELEGRAM_CHAT_ID there is no owner, and nobody may approve -- a
        stricter rule than for questions, because a question costs a reply and
        an approval can cost an email.
        """
        cq = update["callback_query"]
        chat_id = ((cq.get("message") or {}).get("chat") or {}).get("id")
        from_id = (cq.get("from") or {}).get("id")
        if not self._owner or chat_id != self._owner or from_id != self._owner:
            log.warning("ignoring a button press from chat %s / user %s", chat_id, from_id)
            return
        fresh = await self._store.log_message(
            update_id=update.get("update_id"), chat_id=chat_id, direction="in",
            kind="callback", body=(cq.get("data") or "")[:64],
        )
        if not fresh:
            return

        from sloane.reminders import parse_snooze

        snooze = parse_snooze(cq.get("data") or "")
        parsed = parse_callback(cq.get("data") or "")
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Stop the button's spinner whatever happens next.
            try:
                await self._call(client, "answerCallbackQuery", callback_query_id=cq.get("id"))
            except (httpx.HTTPError, RuntimeError) as exc:
                log.warning("answerCallbackQuery failed: %s", exc)
            if snooze is not None:
                message = await self._snooze(*snooze)
                message_id = (cq.get("message") or {}).get("message_id")
                if message_id:
                    try:  # one press per reminder
                        await self._call(
                            client, "editMessageReplyMarkup", chat_id=chat_id,
                            message_id=message_id, reply_markup={"inline_keyboard": []},
                        )
                    except (httpx.HTTPError, RuntimeError):
                        pass
                if message != "👍":
                    await self.send(chat_id, Reply(speech=message, detail=""))
                return
            if parsed is None or self.agency is None:
                return
            outcome = await self.agency.decide(*parsed)
            # Take the buttons off the original message so it cannot be
            # pressed again; the atomic transition already makes that harmless.
            message_id = (cq.get("message") or {}).get("message_id")
            if message_id and outcome.status != "stale":
                try:
                    await self._call(
                        client, "editMessageReplyMarkup", chat_id=chat_id,
                        message_id=message_id, reply_markup={"inline_keyboard": []},
                    )
                except (httpx.HTTPError, RuntimeError):
                    pass
        await self.send(chat_id, Reply(speech=outcome.message, detail=""))

    async def send_voice(self, chat_id: int, ogg: bytes) -> None:
        """Send an OGG/Opus clip as a Telegram voice note."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                self._url("sendVoice"),
                data={"chat_id": str(chat_id)},
                files={"voice": ("sloane.ogg", ogg, "audio/ogg")},
            )
        if response.status_code >= 400 or not response.json().get("ok"):
            raise RuntimeError(f"telegram sendVoice -> {response.status_code}")
        await remember(
            "outbound voice",
            self._store.log_message(chat_id=chat_id, direction="out", kind="voice"),
        )

    async def reply(self, chat_id: int, reply: Reply, *, as_voice: bool) -> None:
        """Answer in the modality he used. Text is the floor, never the risk.

        A voice note in gets a voice note out, reading `speech` only, followed
        by the detail as text when it adds something. If the voice cannot be
        made or sent -- budget, TTS, ffmpeg, Telegram -- the whole reply goes
        as text instead, exactly as it would have without P3.
        """
        if as_voice and self._voice is not None:
            ogg = await self._voice.render(reply.speech)
            if ogg is not None:
                try:
                    await self.send_voice(chat_id, ogg)
                except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                    log.warning("voice note not delivered, falling back to text: %s", exc)
                else:
                    detail = (reply.detail or "").strip()
                    if detail and detail != reply.speech.strip():
                        await self.send(chat_id, Reply(speech="", detail=detail))
                    return
        await self.send(chat_id, reply)

    async def _download_voice(self, client: httpx.AsyncClient, file_id: str) -> bytes:
        meta = await self._call(client, "getFile", file_id=file_id)
        path = meta.get("file_path")
        if not path:
            raise RuntimeError("telegram returned no file_path for the voice note")
        response = await client.get(f"{API}/file/bot{self._token}/{path}")
        # Not raise_for_status(): its message includes the URL, and this URL
        # contains the bot token. It would land in the log and in the chat.
        if response.status_code != 200:
            raise RuntimeError(f"voice download failed: HTTP {response.status_code}")
        return response.content

    # -- commands --------------------------------------------------------------

    async def _usage(self) -> Reply:
        rows = await self._store.usage_summary(24)
        if not rows:
            return Reply(speech="Nothing has gone through a model in the last day.", detail="")
        lines = ["| provider | purpose | calls | fails | degraded | tokens |", "|---|---|---|---|---|---|"]
        total = 0
        for r in rows:
            tokens = int(r["prompt_tokens"] or 0) + int(r["completion_tokens"] or 0)
            total += tokens
            lines.append(
                f"| {r['provider']} | {r['purpose']} | {r['calls']} | "
                f"{r['failures']} | {r['degraded']} | {tokens} |"
            )
        calls = sum(int(r["calls"]) for r in rows)
        return Reply(
            speech=f"{calls} model calls in the last day, about {total} tokens.",
            detail="\n".join(lines),
        )

    async def _state(self) -> Reply:
        rows = await self._store.get_state()
        if not rows:
            return Reply(
                speech="I have no durable state yet. Seed it and I will keep it in every prompt.",
                detail="Run `python scripts/seed_state.py <file.md>`.",
            )
        lines = [f"- **{r['key']}** — {r['value']}" for r in rows]
        return Reply(
            speech=f"I am holding {len(rows)} durable facts about you.",
            detail="\n".join(lines),
        )

    async def _sync(self) -> Reply:
        """Pull every upstream source now, rather than waiting for the job."""
        from sloane.school.sync import sync_all

        from sloane.school.changes import announce

        report = await sync_all(self._store, self._config)
        # He asked, so any Canvas changes go out now, whatever the hour.
        try:
            await announce(self._store, self.say)
        except Exception as exc:  # noqa: BLE001 - the next scheduled sync retries
            log.warning("change alert not delivered: %s", exc)
        return Reply(speech=report.speech(), detail=report.detail())

    async def _brief(self) -> Reply:
        """The morning brief, on demand.

        Goes straight to the agent rather than through the governor: quiet
        hours stop her *starting* a conversation at 1 AM, not answering one he
        started.
        """
        from sloane.jobs.briefs import QUESTIONS

        return await self._agent.answer(QUESTIONS["morning_brief"], channel="command:brief")

    async def _inbox(self) -> Reply:
        """Triage mail now, under the same rules as the scheduled run."""
        if self.run_job is None:
            return Reply(speech="The scheduler isn't running, so I can't check mail.", detail="")
        result = await self.run_job("inbox")
        if result.reply is not None and result.sent:
            # The job already delivered its summary to this chat.
            return Reply(speech="That's everything new in your inbox.", detail=result.reason)
        if result.reply is not None:
            return result.reply
        if not result.ran:
            return Reply(speech="I didn't check your inbox.", detail=result.reason)
        return Reply(speech="Nothing new in your inbox needs you.", detail=result.reason)

    # -- reminders ---------------------------------------------------------------

    def _now(self) -> datetime:
        return datetime.now(ZoneInfo(self._config.timezone))

    async def _remind(self, rest: str) -> Reply:
        """Schedule a reminder from his own words.

        No approval step: this is Landen asking for it directly, and it touches
        nothing outside his own chat. (Anything *she* decides to do still goes
        through the agency.)
        """
        from sloane.reminders import parse, spoken

        how = "Try: remind me at 5 to call Keegan, or /remind tomorrow 7am bring the lab."
        if not rest.strip():
            return Reply(speech="What should I remind you about, and when?", detail=how)
        now = self._now()
        parsed = parse(rest, now)
        if parsed is None:
            return Reply(speech="I couldn't tell when you want that reminder.", detail=how)
        if not parsed.text:
            return Reply(speech="What should the reminder say?", detail=how)
        text = safe_field(parsed.text, limit=300)
        await self._store.add_reminder(text=text, due_at=parsed.due)
        return Reply(speech=f"Okay, I'll remind you {spoken(parsed.due, now)}: {text}.", detail="")

    async def _view(self, name: str) -> Reply:
        """/today and /week, from SQL alone. Answers even with every model down."""
        from datetime import timedelta

        from sloane import views

        day = self._now().date()
        span = 0 if name == "today" else 6
        end = day + timedelta(days=span)
        assignments = await self._store.assignments_due(day, end)
        shifts = await self._store.shifts_between(day, end)
        events = await self._store.events_between(day, end)
        tz = self._config.timezone
        if name == "today":
            overdue = await self._store.overdue_assignments()
            speech, detail = views.today(day, assignments=assignments, shifts=shifts,
                                         events=events, overdue=overdue, tz=tz)
        else:
            speech, detail = views.week(day, assignments=assignments, shifts=shifts,
                                        events=events, tz=tz)
        return Reply(speech=speech, detail=detail)

    async def _promises(self, name: str, rest: str) -> Reply:
        """/promise <what> [to Name] [by when] · /promises · /kept <n>."""
        from sloane.promises import parse as parse_promise
        from sloane.reminders import spoken

        now = self._now()
        zone = ZoneInfo(self._config.timezone)
        if name == "promise":
            promise = parse_promise(rest, now)
            if promise is None:
                return Reply(speech="What did you promise?",
                             detail="Try: /promise send Keegan the outline by friday")
            person_id = await self._store.person_id(promise.person) if promise.person else None
            await self._store.add_commitment(safe_field(promise.what, limit=300), person_id=person_id,
                                             due_at=promise.due)
            when = f", due {spoken(promise.due, now).removeprefix('at ')}" if promise.due else ""
            return Reply(speech=f"Noted: {safe_field(promise.what, limit=300)}{when}.", detail="")

        rows = await self._store.open_commitments()
        if name == "kept":
            if not rest.strip().isdigit() or not 1 <= int(rest) <= len(rows):
                return Reply(speech="Use /kept with a number from /promises.", detail="")
            row = await self._store.close_commitment(str(rows[int(rest) - 1]["id"]))
            if row is None:
                return Reply(speech="That one's already closed.", detail="")
            return Reply(speech=f"Nice. Marked kept: {row['what']}.", detail="")
        if not rows:
            return Reply(speech="No open promises.", detail="")
        lines = []
        for i, r in enumerate(rows, 1):
            who = f" (to {r['person']})" if r.get("person") else ""
            due = f" — due {spoken(r['due_at'].astimezone(zone), now).removeprefix('at ')}" if r.get("due_at") else ""
            lines.append(f"{i}. {r['what']}{who}{due}")
        return Reply(speech=f"{len(rows)} open promise{'s' if len(rows) != 1 else ''}.",
                     detail="\n".join(lines) + "\n\n`/kept <n>` marks one done.")

    async def _status(self) -> Reply:
        """Is she healthy? From her own bookkeeping, no model call."""
        from sloane.reminders import spoken

        now = self._now()
        zone = ZoneInfo(self._config.timezone)
        alerts = await self._store.open_alerts()
        jobs = {j["name"]: j for j in await self._store.jobs()}
        health = await self._store.provider_health(24)
        lines = []
        if alerts:
            lines += [f"⚠️ {a['message']}" for a in alerts]
        sync = jobs.get("entity_sync", {})
        if sync.get("last_run_at"):
            hours = (now - sync["last_run_at"].astimezone(zone)).total_seconds() / 3600
            lines.append(f"• Last school sync: {hours:.0f}h ago ({sync.get('last_status') or '-'})")
        else:
            lines.append("• School sync has not run yet")
        for row in sorted(health, key=lambda r: r["provider"]):
            ok = int(row["calls"]) - int(row["failures"])
            lines.append(f"• {row['provider']}: {ok}/{row['calls']} calls ok in 24h")
        brief = jobs.get("morning_brief", {})
        if brief.get("last_run_at"):
            when = spoken(brief["last_run_at"].astimezone(zone), now).removeprefix("at ")
            lines.append(f"• Last morning brief: {when} ({brief.get('last_status') or '-'})")
        lines.append(f"• Gmail: {'connected' if self._config.gmail_refresh_token else 'not set up'}")
        speech = (f"{len(alerts)} problem{'s' if len(alerts) != 1 else ''} open." if alerts
                  else "All good: nothing is broken that I know of.")
        return Reply(speech=speech, detail="\n".join(lines))

    async def _done(self, rest: str) -> Reply:
        """/done <part of a title>: he turned it in; Canvas just hasn't noticed."""
        words = [w for w in re.findall(r"\w+", rest.lower()) if len(w) > 1]
        if not words:
            return Reply(speech="Which one? Try /done lab writeup.", detail="")
        rows = await self._store.outstanding_assignments()
        hits = [r for r in rows if all(w in r["title"].lower() or w in (r.get("course") or "").lower()
                                       for w in words)]
        if not hits:
            return Reply(speech="I don't see an open assignment like that.", detail="")
        if len(hits) > 1:
            listing = "\n".join(f"• {r['title']}" + (f" [{r['course']}]" if r.get("course") else "")
                                 for r in hits[:10])
            return Reply(speech=f"{len(hits)} match; say a bit more.", detail=listing)
        row = await self._store.mark_done_locally(str(hits[0]["id"]))
        return Reply(speech=f"Marked done: {row['title']}. I'll stop counting it as due.", detail="")

    async def _grades(self) -> Reply:
        """Current course grades as Canvas last reported them. No model."""
        rows = [c for c in await self._store.courses() if c.get("current_score") is not None]
        if not rows:
            return Reply(speech="I don't have any course grades from Canvas yet.",
                         detail="Grades arrive with the Canvas sync, if your teachers show totals. `/sync` pulls now.")
        rows.sort(key=lambda c: c["current_score"])
        lines = []
        for c in sorted(rows, key=lambda c: (c.get("period") is None, c.get("period") or 0)):
            letter = f" ({c['current_grade']})" if c.get("current_grade") else ""
            period = f"P{c['period']} " if c.get("period") is not None else ""
            lines.append(f"• {period}{c['name']}: {c['current_score']:g}%{letter}")
        low = rows[0]
        return Reply(
            speech=f"Lowest right now is {low['name']} at {low['current_score']:g} percent.",
            detail="\n".join(lines),
        )

    async def _reminders(self, command: str) -> Reply:
        from sloane.reminders import spoken

        rows = await self._store.upcoming_reminders(20)
        parts = command.split()
        if parts[0].lstrip("/").split("@")[0].lower() == "unremind":
            if len(parts) != 2 or not parts[1].isdigit() or not 1 <= int(parts[1]) <= len(rows):
                return Reply(speech="Use /unremind with a number from /reminders.", detail="")
            row = await self._store.cancel_reminder(str(rows[int(parts[1]) - 1]["id"]))
            if row is None:
                return Reply(speech="That one already went out.", detail="")
            return Reply(speech=f"Cancelled: {row['text']}.", detail="")
        if not rows:
            return Reply(speech="No reminders set.", detail="")
        now = self._now()
        zone = ZoneInfo(self._config.timezone)
        lines = [f"{i}. {spoken(r['due_at'].astimezone(zone), now)}: {r['text']}"
                 for i, r in enumerate(rows, 1)]
        return Reply(
            speech=f"{len(rows)} reminder{'s' if len(rows) != 1 else ''} set.",
            detail="\n".join(lines) + "\n\n`/unremind <n>` cancels one.",
        )

    async def _agency_command(self, name: str, command: str) -> Reply:
        if self.agency is None:
            return Reply(speech="Actions are off: set TELEGRAM_CHAT_ID so I know who approves.", detail="")
        rest = command.split(maxsplit=1)[1].strip() if " " in command.strip() else ""
        if name == "cancel":
            outcome = await self.agency.cancel_edit()
            return Reply(speech=outcome.message if outcome else "Nothing is being edited.", detail="")
        if name == "revoke":
            parts = rest.split()
            if len(parts) != 2:
                return Reply(speech="Use /revoke <action> <target>, for example /revoke remind self.", detail="")
            outcome = await self.agency.revoke(parts[0], parts[1])
            return Reply(speech=outcome.message, detail="")
        rows = await self._store.trust_ledger()
        lines = ["| action | target | state | streak | reversals |", "|---|---|---|---|---|"]
        for r in rows:
            state = "HARD LINE" if r["hard_line"] else r["state"]
            lines.append(f"| {r['action']} | {r['target']} | {state} | {r['clean_streak']} | {r['reversals']} |")
        trusted = sum(1 for r in rows if r["state"] == "trusted" and not r["hard_line"])
        return Reply(
            speech=f"{trusted} action{'s' if trusted != 1 else ''} trusted to run without asking.",
            detail="\n".join(lines),
        )

    async def _jobs(self) -> Reply:
        rows = await self._store.jobs()
        lines = ["| job | last run | status |", "|---|---|---|"]
        failing = 0
        for r in rows:
            when = r["last_run_at"].strftime("%a %H:%M") if r.get("last_run_at") else "never"
            status = r.get("last_status") or "-"
            if status == "failed":
                failing += 1
            lines.append(f"| {r['name']} | {when} | {status} |")
        speech = (
            f"{failing} job{'s' if failing != 1 else ''} failed on the last run."
            if failing else f"{len(rows)} jobs scheduled, none failing."
        )
        return Reply(speech=speech, detail="\n".join(lines))

    async def _handle_command(self, command: str) -> Reply | None:
        name = command.split()[0].lstrip("/").split("@")[0].lower()
        if name == "usage":
            return await self._usage()
        if name == "state":
            return await self._state()
        if name == "sync":
            return await self._sync()
        if name == "brief":
            return await self._brief()
        if name == "remind":
            rest = command.split(maxsplit=1)[1] if len(command.split(maxsplit=1)) > 1 else ""
            return await self._remind(rest)
        if name in {"promise", "promises", "kept"}:
            rest = command.split(maxsplit=1)[1] if len(command.split(maxsplit=1)) > 1 else ""
            return await self._promises(name, rest)
        if name in {"reminders", "unremind"}:
            return await self._reminders(command)
        if name in {"trust", "revoke", "cancel"}:
            return await self._agency_command(name, command)
        if name == "grades":
            return await self._grades()
        if name == "done":
            rest = command.split(maxsplit=1)[1] if len(command.split(maxsplit=1)) > 1 else ""
            return await self._done(rest)
        if name == "status":
            return await self._status()
        if name in {"today", "week"}:
            return await self._view(name)
        if name == "jobs":
            return await self._jobs()
        if name == "inbox":
            return await self._inbox()
        if name in {"start", "help"}:
            return Reply(
                speech="I am here. Text me or send a voice note.",
                detail=(
                    "`/usage` — model calls in the last 24h\n"
                    "`/state` — the durable facts I hold\n"
                    "`/sync` — pull Canvas, the calendar and shifts now\n"
                    "`/today` · `/week` — the schedule straight from the database, no AI\n"
                    "`/grades` — current course grades from Canvas\n"
                    "`/done <assignment>` — handed it in; stop counting it as due\n"
                    "`/status` — is anything broken? (no AI)\n"
                    "`/brief` — the morning brief, right now\n"
                    "`/jobs` — what ran, and whether it worked\n"
                    "`/inbox` — triage new email now\n"
                    "`/remind 5pm call Keegan` — a reminder at a time (or just say \"remind me…\")\n"
                    "`/reminders` — what's set; `/unremind <n>` cancels one\n"
                    "`/promise <what> by <when>` — track a promise; `/promises`, `/kept <n>`\n"
                    "`/trust` — what I may do without asking\n"
                    "`/revoke <action> <target>` — make me ask again"
                ),
            )
        return None

    # -- the loop --------------------------------------------------------------

    async def _handle(self, update: dict) -> None:
        if "callback_query" in update:
            await self._handle_callback(update)
            return
        update_id = update.get("update_id")
        message = update.get("message") or update.get("edited_message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        if chat_id is None:
            return

        # Single-user assistant. An unknown chat is dropped without a reply, so
        # the bot cannot be enumerated by strangers who guess the handle.
        if self._owner and chat_id != self._owner:
            log.warning("ignoring message from unexpected chat %s", chat_id)
            return
        # No owner configured yet: fail closed. The one thing anyone gets back
        # is their own chat id, which is exactly what setup needs -- never her
        # schedule, state or inbox to whoever found the bot first.
        if not self._owner:
            log.warning("TELEGRAM_CHAT_ID is unset; told chat %s its id and nothing else", chat_id)
            await self.send(chat_id, Reply(
                speech="I'm not set up yet.",
                detail=f"Your chat id is `{chat_id}`. Put `TELEGRAM_CHAT_ID={chat_id}` "
                       "in .env and restart me.",
            ))
            return

        voice = message.get("voice") or message.get("audio")
        kind = "voice" if voice else "text"
        body = (message.get("text") or message.get("caption") or "").strip()

        # The cursor doubles as the dedupe check: a replayed update_id is a
        # no-op, so a crash between receiving and answering cannot double-answer.
        fresh = await self._store.log_message(
            update_id=update_id,
            chat_id=chat_id,
            direction="in",
            kind=kind,
            body=body or None,
            file_id=voice.get("file_id") if voice else None,
        )
        if not fresh:
            log.info("update %s already handled, skipping", update_id)
            return

        if voice:
            try:
                async with httpx.AsyncClient(timeout=120.0) as client:
                    audio = await self._download_voice(client, voice["file_id"])
                body = await self._stt.transcribe(audio)
            except (ProviderError, RuntimeError, httpx.HTTPError) as exc:
                log.warning("transcription failed: %s", exc)
                await self.send(
                    chat_id,
                    Reply(
                        speech="I could not make out that voice note.",
                        detail=f"Transcription failed: {exc}",
                    ),
                )
                return
            if not body:
                await self.send(
                    chat_id,
                    Reply(speech="That voice note came back empty.", detail=""),
                )
                return

        if not body:
            return

        # While a proposal is being edited, his next plain message is the
        # replacement, not a question. /cancel keeps the original.
        if self.agency is not None and body and not body.startswith("/"):
            revised = await self.agency.submit_edit(body)
            if revised is not None:
                await self.send(chat_id, Reply(speech=revised.message, detail=""))
                return

        # "Remind me at 5 to call Keegan" -- typed or spoken -- is handled by
        # rules, not the model, so it works when every provider is down.
        asked = REMIND_ME.match(body)
        if asked:
            await self.send(chat_id, await self._remind(asked.group(1)))
            return

        if body.startswith("/"):
            reply = await self._handle_command(body)
            if reply is not None:
                await self.send(chat_id, reply)
                return

        reply = await self._agent.answer(body, channel=kind)
        await self.reply(chat_id, reply, as_voice=bool(voice))

    async def poll_forever(self) -> None:
        """Long-poll until stopped. Network trouble backs off, it does not exit."""
        if not self._token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

        offset = await self._store.next_update_offset()
        log.info("long polling from offset %s", offset)
        backoff = 1.0
        read_timeout = self._config.telegram_poll_timeout + TIMEOUT_MARGIN

        async with httpx.AsyncClient(timeout=read_timeout) as client:
            while not self._stop.is_set():
                try:
                    updates = await self._call(
                        client,
                        "getUpdates",
                        offset=offset,
                        timeout=self._config.telegram_poll_timeout,
                        allowed_updates=["message", "edited_message", "callback_query"],
                    )
                    backoff = 1.0
                except (httpx.HTTPError, RuntimeError) as exc:
                    log.warning("getUpdates failed, retrying in %.0fs: %s", backoff, exc)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60.0)
                    continue

                for update in updates if isinstance(updates, list) else []:
                    offset = max(offset, int(update.get("update_id", 0)) + 1)
                    try:
                        await self._handle(update)
                    except Exception:  # noqa: BLE001 - one bad update must not stop the bot
                        log.exception("failed to handle update %s", update.get("update_id"))

    def stop(self) -> None:
        self._stop.set()
