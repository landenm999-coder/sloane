"""FastAPI process: /health and /usage, with the Telegram poller alongside.

One process runs both. The bot is a background task rather than a second
service because the box is a 2-core ARM instance and a second Python
interpreter would cost ~80 MB to save nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import date, timedelta

from fastapi import FastAPI

from sloane.agent import HARD_LINES, Agent
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
        agent = Agent(store, config, embedder=embedder)
        state["agent"] = agent
        state["embedder"] = embedder

        # Load the ONNX weights now so the first message of the day is not the
        # one that waits for a 130 MB model to come off disk.
        asyncio.create_task(embedder.warm())

        task: asyncio.Task | None = None
        if config.telegram_bot_token:
            bot = Bot(store, agent, config)
            state["bot"] = bot
            task = asyncio.create_task(bot.poll_forever())
            log.info("telegram poller started")
        else:
            log.warning("TELEGRAM_BOT_TOKEN is unset; running without the bot")

        try:
            yield
        finally:
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

    @app.get("/facts")
    async def facts(days: int = 7) -> dict:
        """Tier 4 as JSON. What she would answer from, without the model."""
        today = date.today()
        horizon = today + timedelta(days=days)
        return {
            "due": [dict(r) for r in await store.assignments_due(today, horizon)],
            "overdue": [dict(r) for r in await store.overdue_assignments()],
            "shifts": [dict(r) for r in await store.shifts_between(today, horizon)],
            "commitments": [dict(r) for r in await store.open_commitments()],
        }

    return app


app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000, log_config=None)


if __name__ == "__main__":
    main()
