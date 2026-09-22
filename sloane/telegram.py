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

import httpx

from sloane.agent import Agent
from sloane.config import Settings, settings as default_settings
from sloane.contract import Reply
from sloane.memory.store import Store, remember
from sloane.providers.base import ProviderError
from sloane.providers.groq import GroqProvider
from sloane.voice import Voice

log = logging.getLogger(__name__)

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
    ) -> None:
        self._config = config or default_settings()
        self._store = store
        self._agent = agent
        self._voice = voice
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
            await self._call(client, "sendMessage", chat_id=chat_id, text=text[:4096])
        await remember(
            "outbound message",
            self._store.log_message(
                chat_id=chat_id, direction="out", kind="text", body=text[:4000]
            ),
        )

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
        response.raise_for_status()
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

        report = await sync_all(self._store, self._config)
        return Reply(speech=report.speech(), detail=report.detail())

    async def _brief(self) -> Reply:
        """The morning brief, on demand.

        Goes straight to the agent rather than through the governor: quiet
        hours stop her *starting* a conversation at 1 AM, not answering one he
        started.
        """
        from sloane.jobs.briefs import QUESTIONS

        return await self._agent.answer(QUESTIONS["morning_brief"], channel="command:brief")

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
        if name == "jobs":
            return await self._jobs()
        if name in {"start", "help"}:
            return Reply(
                speech="I am here. Text me or send a voice note.",
                detail=(
                    "`/usage` — model calls in the last 24h\n"
                    "`/state` — the durable facts I hold\n"
                    "`/sync` — pull Canvas, the calendar and shifts now\n"
                    "`/brief` — the morning brief, right now\n"
                    "`/jobs` — what ran, and whether it worked"
                ),
            )
        return None

    # -- the loop --------------------------------------------------------------

    async def _handle(self, update: dict) -> None:
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
                        allowed_updates=["message", "edited_message"],
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
