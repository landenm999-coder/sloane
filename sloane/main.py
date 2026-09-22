"""FastAPI process: /health and /usage, with the Telegram poller alongside.

One process runs both. The bot is a background task rather than a second
service because the box is a 2-core ARM instance and a second Python
interpreter would cost ~80 MB to save nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import FastAPI

from sloane.agency import Agency, reminder_action
from sloane.agent import HARD_LINES, Agent
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
from sloane.telegram import Bot

log = logging.getLogger(__name__)


def create_app() -> FastAPI:
    config = settings()
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    store = Store(config)
    state: dict = {}

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        await store.open()
        embedder = Embedder(config)
        # One router for text and speech, so /usage accounts for both.
        router = Router(config, usage_sink=usage_sink(store))
        agent = Agent(store, config, router=router, embedder=embedder)
        voice = Voice(router, store, config)
        state["agent"] = agent
        state["embedder"] = embedder

        # Load the ONNX weights now so the first message of the day is not the
        # one that waits for a 130 MB model to come off disk.
        asyncio.create_task(embedder.warm())

        task: asyncio.Task | None = None
        bot: Bot | None = None
        if config.telegram_bot_token:
            bot = Bot(store, agent, config, voice=voice)
            state["bot"] = bot
            task = asyncio.create_task(bot.poll_forever())
            log.info("telegram poller started")
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
        send = None
        if bot is not None and config.telegram_chat_id:
            async def deliver(reply):  # noqa: ANN001
                await bot.send(config.telegram_chat_id, reply)

            send = deliver

        ctx = JobContext(
            store=store, agent=agent, governor=Governor(store, config),
            config=config, send=send, inbox=inbox,
        )
        scheduler = Scheduler(ctx)
        state["scheduler"] = scheduler
        if bot is not None:
            bot.run_job = scheduler.run
        try:
            await scheduler.start()
        except Exception:  # noqa: BLE001 - no scheduler is bad; no bot is worse
            log.exception("scheduler failed to start; replies still work")

        try:
            yield
        finally:
            scheduler.stop()
            if task is not None:
                state["bot"].stop()
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await store.close()

    app = FastAPI(title="Sloane", version="0.1.0", lifespan=lifespan)

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
