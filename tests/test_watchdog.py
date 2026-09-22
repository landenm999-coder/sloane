"""The watchdog: told once after a grace period, repeated daily, cleared when fixed.

DESTRUCTIVE: resets jobs bookkeeping and truncates alerts and usage_log.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _settings import isolated

from sloane.jobs.briefs import JobContext, watchdog as watchdog_job
from sloane.jobs.governor import Governor
from sloane.jobs.watchdog import problems
from sloane.memory.store import Store

FAILURES: list[str] = []
DEN = ZoneInfo("America/Denver")
T0 = datetime(2026, 9, 22, 9, 0, tzinfo=DEN)


def check(label: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{label}\n     got: {got!r}\n    want: {want!r}")


# -- detection is pure ----------------------------------------------------------
config = isolated(timezone="America/Denver", main_provider="claude_code", bulk_provider="groq")
jobs = [
    {"name": "morning_brief", "last_status": "failed", "last_error": "boom", "enabled": True},
    {"name": "entity_sync", "last_status": "partial", "last_error": "canvas: 401", "enabled": True},
    {"name": "inbox", "last_status": "deferred", "enabled": True,
     "last_error": "inbox not triaged: Gmail access was revoked or expired. Run scripts/gmail_auth.py again"},
    {"name": "wrap", "last_status": "deferred", "last_error": "quiet hours until 6:30", "enabled": True},
    {"name": "old", "last_status": "failed", "last_error": "x", "enabled": False},
]
health = [
    {"provider": "claude_code", "calls": 4, "failures": 4},
    {"provider": "groq", "calls": 10, "failures": 2},
    {"provider": "anthropic", "calls": 5, "failures": 5},  # not a configured lane
]
found = problems(jobs, health, config)
check("the right problems, and only those", sorted(found),
      ["gmail", "job:entity_sync", "job:morning_brief", "provider:claude_code"])
check("an expired Claude login says how to fix it", "docker compose run --rm sloane claude" in found["provider:claude_code"], True)
check("a deferral for quiet hours is not a problem", "job:wrap" in found, False)
check("a mostly-working provider is not a problem", "provider:groq" in found, False)


async def integration() -> None:
    cfg = isolated(database_url=os.environ["DATABASE_URL"], timezone="America/Denver",
                   main_provider="claude_code", bulk_provider="groq")
    async with Store(cfg) as store:
        await store._exec("truncate alerts, usage_log restart identity")
        await store._exec("update jobs set last_status = null, last_error = null, enabled = true")
        said: list[str] = []

        async def say(text):
            said.append(text)

        ctx = JobContext(store=store, agent=None, governor=Governor(store, cfg), config=cfg, say=say)

        async def tick(at):
            return await watchdog_job(ctx, at)

        await tick(T0)
        check("all healthy: silence", said, [])

        await store.mark_job("morning_brief", status="failed", error="model unreachable")
        await tick(T0)
        check("a new problem waits out the grace period", said, [])
        await tick(T0 + timedelta(minutes=30))
        check("still inside it", said, [])
        await tick(T0 + timedelta(minutes=61))
        check("then it is told once", said, ["⚠️ The morning_brief job is failing: model unreachable"])
        await tick(T0 + timedelta(hours=5))
        check("and not repeated within the day", len(said), 1)
        await tick(T0 + timedelta(hours=25, minutes=2))
        check("but repeated daily while it lasts", said[-1], "⚠️ Still: The morning_brief job is failing: model unreachable")

        night = await tick(datetime(2026, 9, 24, 2, 0, tzinfo=DEN))
        check("quiet hours hold the check", night.ran, False)

        await store.mark_job("morning_brief", status="ok")
        await tick(T0 + timedelta(hours=26))
        check("recovery is told", said[-1], "✅ The morning_brief job is working again.")
        count = len(said)
        await tick(T0 + timedelta(hours=27))
        check("and then silence", len(said), count)

        # A blip that clears inside the grace period is never mentioned at all.
        await store.mark_job("entity_sync", status="partial", error="canvas timeout")
        await tick(T0 + timedelta(hours=28))
        await store.mark_job("entity_sync", status="ok")
        await tick(T0 + timedelta(hours=28, minutes=30))
        check("a blip is never told, nor its recovery", len(said), count)

        # It can come back later as a fresh problem, with a fresh grace period.
        await store.mark_job("entity_sync", status="partial", error="canvas 401")
        await tick(T0 + timedelta(hours=30))
        await tick(T0 + timedelta(hours=31, minutes=1))
        check("a returning problem is told again", said[-1], "⚠️ The entity_sync job only partly worked: canvas 401")

        # Provider failure from the usage log: an expired Claude login.
        await store.mark_job("entity_sync", status="ok")
        for _ in range(3):
            await store.log_usage(provider="claude_code", ok=False, error="not logged in")
        await tick(T0 + timedelta(hours=32))
        await tick(T0 + timedelta(hours=33, minutes=1))
        check("an all-failing main lane is told with the fix",
              "the Claude CLI login has probably expired" in said[-1], True)


asyncio.run(integration())

if FAILURES:
    print(f"FAIL ({len(FAILURES)})")
    for f in FAILURES:
        print("  -", f)
    raise SystemExit(1)
print("watchdog: grace, once, daily repeat, recovery, blips ignored, provider health")
