#!/usr/bin/env python3
"""Validate every credential and say exactly what is missing.

Run this after touching anything with a key in it. It never guesses: each check
reports PASS, FAIL or SKIP with the precise remedy, and the exit code is
non-zero only if something Sloane actually needs is broken.

    python scripts/doctor.py           # report only, downloads nothing
    python scripts/doctor.py --warm    # also fetch and verify the embedder
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sloane.config import Settings, settings as load_settings
from sloane.providers.base import ProviderError
from sloane.router import MAIN_ORDER, build

EXPECTED_TABLES = {
    "assignments", "commitments", "courses", "episodes", "jobs", "messages",
    "people", "shifts", "state", "trust", "usage_log", "working_set",
}

PASS, FAIL, SKIP, WARN = "PASS", "FAIL", "SKIP", "WARN"

results: list[tuple[str, str, str]] = []


def record(name: str, status: str, detail: str = "") -> None:
    results.append((name, status, detail))


async def check_database(config: Settings) -> None:
    if not config.database_url:
        record("database", FAIL, "DATABASE_URL is unset. Copy it from Supabase → Project Settings → Database.")
        return
    try:
        from sloane.memory.store import Store
    except ImportError as exc:
        record("database", FAIL, f"psycopg is not installed: {exc}. Run pip install -r requirements.txt")
        return

    store = Store(config)
    try:
        await store.open()
    except Exception as exc:  # noqa: BLE001 - every connection failure is a report
        record("database", FAIL, f"could not connect: {exc}")
        return

    try:
        record("database", PASS, "connected")

        rows = await store._fetch(
            "select table_name from information_schema.tables where table_schema = 'public'"
        )
        found = {r["table_name"] for r in rows}
        missing = EXPECTED_TABLES - found
        if missing:
            record("schema", FAIL, f"missing {sorted(missing)}. Apply sql/001_init.sql.")
        else:
            record("schema", PASS, f"{len(EXPECTED_TABLES)} tables present")

        ext = await store._fetch("select extversion from pg_extension where extname = 'vector'")
        if not ext:
            record("pgvector", FAIL, "the vector extension is not installed. Enable it in Supabase → Database → Extensions.")
        else:
            version = ext[0]["extversion"]
            record("pgvector", PASS, f"{version} (schema uses vector(384), never halfvec)")

        lines = await store.hard_lines()
        if len(lines) == 6:
            record("hard lines", PASS, "6 rows recorded (the gate itself lives in agent.py)")
        else:
            record("hard lines", FAIL, f"{len(lines)} rows, expected 6. Re-apply sql/001_init.sql.")

        jobs = await store.jobs()
        record(
            "jobs" if len(jobs) == 5 else "jobs",
            PASS if len(jobs) == 5 else WARN,
            f"{len(jobs)} seeded" + ("" if len(jobs) == 5 else ", expected 5"),
        )

        state = await store.get_state()
        if state:
            record("tier 1 state", PASS, f"{len(state)} durable facts")
        else:
            record("tier 1 state", WARN, "empty. Run python scripts/seed_state.py <file.md>")
    finally:
        await store.close()


async def check_provider(config: Settings, name: str, label: str, *, bulk: bool) -> None:
    if name not in MAIN_ORDER:
        record(label, FAIL, f"{name!r} is not a provider. Use one of {', '.join(MAIN_ORDER)}.")
        return

    if name == "claude_code":
        if shutil.which(config.claude_cli) is None:
            record(label, FAIL, f"{config.claude_cli} is not on PATH. Install the Claude Code CLI.")
            return
    if name == "groq" and not config.groq_api_key:
        record(label, FAIL, "GROQ_API_KEY is unset. Get one at console.groq.com/keys")
        return
    if name == "anthropic" and not config.anthropic_api_key:
        record(label, FAIL, "ANTHROPIC_API_KEY is unset. Get one at console.anthropic.com")
        return

    provider = build(name, config, bulk=bulk)
    try:
        completion = await provider.complete("Reply with the single word: ok", "ok", max_tokens=16)
    except ProviderError as exc:
        record(label, FAIL, f"{name} is configured but the call failed: {exc.message}")
        return
    record(label, PASS, f"{name} answered ({completion.usage.model or 'model unreported'})")


async def check_telegram(config: Settings) -> None:
    if not config.telegram_bot_token:
        record("telegram", FAIL, "TELEGRAM_BOT_TOKEN is unset. Create a bot with @BotFather.")
        return
    import httpx

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(
                f"https://api.telegram.org/bot{config.telegram_bot_token}/getMe"
            )
        body = response.json()
    except Exception as exc:  # noqa: BLE001
        record("telegram", FAIL, f"could not reach Telegram: {exc}")
        return
    if not body.get("ok"):
        record("telegram", FAIL, f"the token was rejected: {body.get('description')}")
        return
    handle = (body.get("result") or {}).get("username", "?")
    record("telegram", PASS, f"@{handle}")

    if not config.telegram_chat_id:
        record(
            "telegram chat id",
            WARN,
            "TELEGRAM_CHAT_ID is unset, so she will answer anyone who finds the bot. "
            "Message the bot, then read the id from getUpdates.",
        )
    else:
        record("telegram chat id", PASS, f"locked to {config.telegram_chat_id}")


def check_embedder(config: Settings, *, warm: bool) -> None:
    """Verify the embedder.

    Without --warm this only reports whether the weights are already cached,
    because loading them the first time pulls ~130 MB from Hugging Face and a
    health check should not do that behind your back.
    """
    try:
        from fastembed import TextEmbedding  # noqa: F401
    except ImportError:
        record(
            "embeddings",
            FAIL,
            "fastembed is not installed. Run pip install -r requirements.txt "
            "(episodes still store, but recall stays empty).",
        )
        return

    from sloane.memory.embed import Embedder, EmbedUnavailable

    embedder = Embedder(config)
    if not config.embed_cache_dir:
        record(
            "embed cache",
            WARN,
            "EMBED_CACHE_DIR is unset, so a container rebuild re-downloads "
            "~130 MB. Point it at a mounted volume.",
        )
    else:
        record(
            "embed cache",
            PASS if embedder.cached() else WARN,
            f"{config.embed_cache_dir}"
            + ("" if embedder.cached() else " (empty — first run will download)"),
        )

    if not warm and not embedder.cached():
        record(
            "embeddings",
            SKIP,
            "not downloaded yet. Run `python scripts/doctor.py --warm` once, "
            "on a machine that can reach huggingface.co, to fetch and verify them.",
        )
        return

    try:
        vector = embedder.embed_sync(["doctor check"])[0]
    except EmbedUnavailable as exc:
        record("embeddings", FAIL, str(exc))
        return

    if len(vector) != config.embed_dim:
        record(
            "embeddings",
            FAIL,
            f"{config.embed_model} returned {len(vector)} dims but the schema "
            f"column is vector({config.embed_dim}). Change EMBED_DIM and the "
            f"schema together, or use a {config.embed_dim}-dim model.",
        )
        return
    record(
        "embeddings",
        PASS,
        f"{config.embed_model}, {len(vector)} dims, verified against vector({config.embed_dim})",
    )


def check_timezone(config: Settings) -> None:
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(config.timezone)
    except Exception as exc:  # noqa: BLE001
        record("timezone", FAIL, f"{config.timezone!r} is not a zone: {exc}")
        return
    record("timezone", PASS, config.timezone)


async def main(warm: bool = False) -> int:
    config = load_settings()

    print(f"Sloane doctor — main={config.main_provider} bulk={config.bulk_provider}\n")

    check_timezone(config)
    await check_database(config)
    check_embedder(config, warm=warm)
    await check_provider(config, config.main_provider, "main provider", bulk=False)
    await check_provider(config, config.bulk_provider, "bulk provider", bulk=True)
    await check_telegram(config)

    width = max(len(name) for name, _, _ in results)
    for name, status, detail in results:
        mark = {PASS: "ok  ", FAIL: "FAIL", SKIP: "skip", WARN: "warn"}[status]
        print(f"  [{mark}] {name.ljust(width)}  {detail}")

    failures = [n for n, s, _ in results if s == FAIL]
    warnings = [n for n, s, _ in results if s == WARN]
    print()
    if failures:
        print(f"{len(failures)} blocking: {', '.join(failures)}")
    if warnings:
        print(f"{len(warnings)} to look at: {', '.join(warnings)}")
    if not failures and not warnings:
        print("Everything checks out.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main("--warm" in sys.argv)))
