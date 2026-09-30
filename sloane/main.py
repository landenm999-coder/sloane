"""FastAPI process: /health and /usage, with the Telegram poller alongside.

One process runs both. The bot is a background task rather than a second
service because the box is a 2-core ARM instance and a second Python
interpreter would cost ~80 MB to save nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder as jsonable
from fastapi.responses import HTMLResponse, JSONResponse

from sloane.agency import Agency, reminder_action
from sloane.agent import HARD_LINES, Agent
from sloane.cors import ScopedCORS, origins as cors_origins
from sloane.jobs.briefs import JobContext
from sloane.jobs.governor import Governor
from sloane.jobs.scheduler import Scheduler
from sloane.mail.gmail import GmailClient
from sloane.mail.inbox import Inbox, draft_action, reply_action
from sloane.memory.tiers import usage_sink
from sloane.router import Router
from sloane.voice import Voice
from sloane.config import settings
from sloane.memory.embed import Embedder
from sloane.memory.store import Store
from sloane.skills import SkillContext, load as load_skills
from sloane.telegram import Bot
from sloane.redact import install as redact_install

log = logging.getLogger(__name__)

MAX_CAPTURE_BYTES = 64_000

# Endpoints a browser app may call cross-origin. Each checks its own token.
CORS_PATHS = frozenset({"/capture"})


def create_app() -> FastAPI:
    config = settings()
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # httpx logs every request URL at INFO. Two of ours are credentials: the
    # Telegram API URL embeds the bot token, and the calendar's secret .ics URL
    # *is* the credential. Its warnings and errors still come through.
    # apscheduler logs every run of the every-minute reminders tick at INFO.
    for noisy in ("httpx", "httpcore", "apscheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # And whatever a log line says, a password, card number or key in it is scrubbed first.
    redact_install()

    store = Store(config)
    state: dict = {}

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        await store.open()
        embedder = Embedder(config)
        # One router for text and speech, so /usage accounts for both.
        router = Router(config, usage_sink=usage_sink(store))
        # Skills see the same store, router and embedder as everything else;
        # `say` is filled in once the bot exists.
        skill_ctx = SkillContext(store=store, config=config, router=router, embedder=embedder)
        skills = load_skills(skill_ctx)
        agent = Agent(store, config, router=router, embedder=embedder, skills=skills)
        voice = Voice(router, store, config)
        state["agent"] = agent
        state["router"] = router  # the control room speaks through it (/api/speak)
        state["embedder"] = embedder
        state["skills"] = skills

        # Load the ONNX weights now so the first message of the day is not the
        # one that waits for a 130 MB model to come off disk. Likewise the model
        # lane: a claude process started now answers the first message warm,
        # and a local voice is fetched and loaded before the first voice note.
        asyncio.create_task(embedder.warm())
        asyncio.create_task(agent.prewarm())
        asyncio.create_task(router.prewarm_voice())

        task: asyncio.Task | None = None
        bot: Bot | None = None
        if config.telegram_bot_token:
            bot = Bot(store, agent, config, voice=voice, skills=skills)
            state["bot"] = bot
            if config.telegram_chat_id:
                skill_ctx.say = bot.say
            task = asyncio.create_task(bot.poll_forever())
            log.info("telegram poller started")
            if config.telegram_chat_id:
                # "I'm up", once per version: the sign an install or upgrade worked.
                # After a workshop deploy, "Live: …" says it instead.
                from pathlib import Path

                from sloane.hello import announce

                deployed = (Path(config.deploy_dir) / "result.json").exists()
                asyncio.create_task(announce(store, bot.say, silent=deployed))
                asyncio.create_task(bot.set_menu())
        else:
            log.warning("TELEGRAM_BOT_TOKEN is unset; running without the bot")

        # Actions need someone to approve them. No owner chat, no agency.
        agency: Agency | None = None
        if bot is not None and config.telegram_chat_id:
            agency = Agency(store, config, ask=bot.ask, tell=bot.say)
            agency.register(reminder_action(bot.say))
            bot.agency = agency

        # Gmail reads without asking and never sends without asking. Without an
        # agency there is nobody to ask, so it triages and drafts nothing.
        inbox: Inbox | None = None
        gmail = GmailClient(config)
        if gmail.configured:
            if agency is not None:
                agency.register(reply_action(gmail, config))
                agency.register(draft_action(gmail))
            inbox = Inbox(store, config, gmail=gmail, router=router, agency=agency,
                          embedder=embedder)
            log.info("gmail configured; inbox job active")

        # Briefs go to Landen's chat and nowhere else. Without a chat id there is
        # nobody to send to, so the jobs still run and record, but deliver
        # nothing -- which /jobs will show plainly.
        send = speak = None
        if bot is not None and config.telegram_chat_id:
            async def deliver(reply):  # noqa: ANN001
                await bot.send(config.telegram_chat_id, reply)

            send = deliver

            async def voice_note(reply):  # noqa: ANN001 - voice note, text if voice fails
                await bot.reply(config.telegram_chat_id, reply, as_voice=True)

            speak = voice_note

        # The workshop: features she builds on herself, each waiting for his Accept.
        from sloane.workshop import Workshop

        workshop = Workshop(store, config, router,
                            say=bot.say if bot is not None and config.telegram_chat_id else None)
        state["workshop"] = workshop
        watcher = asyncio.create_task(workshop.watch_deploys())

        ctx = JobContext(
            store=store, agent=agent, governor=Governor(store, config), workshop=workshop,
            config=config, send=send, speak=speak, inbox=inbox,
            say=bot.say if bot is not None and config.telegram_chat_id else None,
            remind=bot.remind if bot is not None and config.telegram_chat_id else None,
            skills=skills,
        )
        scheduler = Scheduler(ctx)
        state["scheduler"] = scheduler
        if bot is not None:
            bot.run_job = scheduler.run
        # The control room's chat answers through the same Bot.respond as
        # Telegram; with no bot token it gets a bot that never polls.
        responder = bot if bot is not None else Bot(store, agent, config, voice=voice, skills=skills)
        responder.run_job = scheduler.run
        state["responder"] = responder
        try:
            await scheduler.start()
            # Canvas, the calendar and shifts now, if the last sync is missing
            # or stale: a fresh install shouldn't wait for the next 4-hour slot.
            asyncio.create_task(scheduler.catch_up("entity_sync", timedelta(hours=4)))
        except Exception:  # noqa: BLE001 - no scheduler is bad; no bot is worse
            log.exception("scheduler failed to start; replies still work")

        try:
            yield
        finally:
            watcher.cancel()
            scheduler.stop()
            if task is not None:
                state["bot"].stop()
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            # The last replies' memory writes, then no CLI left waiting.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(agent.settle(), timeout=10)
            from sloane.providers.claude_code import ClaudeCodeProvider

            ClaudeCodeProvider.close_all()
            await store.close()

    app = FastAPI(title="Sloane", version="0.1.0", lifespan=lifespan)
    # Browser apps (Capture) may call the token-checked endpoints, and only those.
    app.add_middleware(ScopedCORS, origins=cors_origins(config.cors_origins), paths=CORS_PATHS)
    # The control room: /app and /api (sloane/web.py). Off without DASHBOARD_TOKEN.
    from sloane import web

    web.install(app, state, store, config)

    @app.get("/health")
    async def health() -> dict:
        db = await store.healthy()
        return {
            "ok": db,
            "database": "up" if db else "down",
            "main_provider": config.main_provider,
            "bulk_provider": config.bulk_provider,
            "bot": "polling" if "bot" in state else "off",
            "hard_lines": len(HARD_LINES),
            "skills": state["skills"].names if "skills" in state else [],
        }

    @app.get("/usage")
    async def usage(hours: int = 24) -> dict:
        rows = await store.usage_summary(hours)
        return {
            "hours": hours,
            "calls": sum(int(r["calls"]) for r in rows),
            "failures": sum(int(r["failures"]) for r in rows),
            "degraded": sum(int(r["degraded"]) for r in rows),
            "tokens": sum(
                int(r["prompt_tokens"] or 0) + int(r["completion_tokens"] or 0)
                for r in rows
            ),
            "by_lane": [dict(r) for r in rows],
        }

    @app.get("/state")
    async def read_state() -> dict:
        rows = await store.get_state()
        return {"count": len(rows), "state": [dict(r) for r in rows]}

    @app.post("/sync")
    async def sync() -> dict:
        """Run the entity sync now. Read-only against every upstream system."""
        from sloane.school.sync import sync_all

        report = await sync_all(store, config)
        return {
            "ok": report.ok,
            "written": report.written,
            "sources": [
                {"name": s.name, "ok": s.ok, "written": s.written, "detail": s.detail}
                for s in report.sources
            ],
        }

    @app.post("/capture")
    async def capture(request: Request) -> JSONResponse:
        """Capture's intake. The only endpoint that checks a credential; see capture.py."""
        from sloane.capture import ingest

        too_big = JSONResponse({"error": "body too large"}, status_code=413)
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > MAX_CAPTURE_BYTES:
            return too_big
        # Read in chunks and stop at the cap: a chunked upload with no length
        # must not be able to fill the box's memory before we look at it.
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_CAPTURE_BYTES:
                return too_big
            chunks.append(chunk)
        raw = b"".join(chunks)
        try:
            payload = json.loads(raw or b"null")
        except ValueError:
            payload = None
        result = await ingest(store, config, payload, request.headers.get("authorization"),
                              embedder=state.get("embedder"), skills=state.get("skills"))
        return JSONResponse(result.body, status_code=result.status)

    @app.get("/jobs")
    async def jobs() -> dict:
        rows = await store.jobs()
        upcoming = state["scheduler"].next_runs() if "scheduler" in state else {}
        return {
            "jobs": [
                {**dict(r), "next_run": upcoming.get(r["name"])} for r in rows
            ]
        }

    @app.post("/jobs/{name}/run")
    async def run_job(name: str) -> dict:
        """Run a job now. Loopback only, like everything here."""
        if "scheduler" not in state:
            return {"ok": False, "reason": "scheduler is not running"}
        result = await state["scheduler"].run(name)
        return {
            "ok": result.ran, "sent": result.sent, "reason": result.reason,
            "speech": result.reply.speech if result.reply else None,
        }

    @app.get("/panels")
    async def panels() -> dict:
        """What each skill would put on the TV, as JSON."""
        if "skills" not in state:
            return {}
        return jsonable(await state["skills"].panels())

    @app.get("/tv")
    async def tv() -> HTMLResponse:
        """The wall dashboard. From SQL and skill panels only; no model."""
        from sloane import dashboard

        data = await dashboard.collect(store, config, state.get("skills"),
                                       datetime.now(ZoneInfo(config.timezone)))
        return HTMLResponse(dashboard.render(data), headers=dashboard.HEADERS)

    @app.get("/facts")
    async def facts(days: int = 7) -> dict:
        """Tier 4 as JSON. What she would answer from, without the model."""
        today = datetime.now(ZoneInfo(config.timezone)).date()
        horizon = today + timedelta(days=days)
        return {
            "due": [dict(r) for r in await store.assignments_due(today, horizon)],
            "overdue": [dict(r) for r in await store.overdue_assignments()],
            "shifts": [dict(r) for r in await store.shifts_between(today, horizon)],
            "commitments": [dict(r) for r in await store.open_commitments()],
            "events": [dict(r) for r in await store.events_between(today, horizon)],
        }

    return app


app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run(app, host=settings().bind_host, port=8000, log_config=None)


if __name__ == "__main__":
    main()
