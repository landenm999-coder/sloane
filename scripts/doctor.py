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
    "people", "proposals", "shifts", "state", "trust", "usage_log", "working_set", "events",
    "emails", "reminders", "school_changes", "alerts", "skill_sessions", "nudges_said",
    "list_items", "countdowns", "cards", "card_reviews", "habits", "habit_log",
    "clients", "client_notes", "focus_sessions", "expenses", "skill_settings",
    "colleges", "college_tasks", "roleplays", "capture_refs", "workshop_items", "dashboard_prefs",
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
            record("schema", FAIL, f"missing {sorted(missing)}. Apply every sql/00*.sql in order (all idempotent).")
        else:
            record("schema", PASS, f"{len(EXPECTED_TABLES)} tables present")

        exposed = await store._fetch(
            "select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace "
            "where n.nspname = 'public' and c.relkind in ('r', 'p') and not c.relrowsecurity"
        )
        if exposed:
            names = ", ".join(sorted(r["relname"] for r in exposed)[:5])
            record("api lockdown", WARN, f"{len(exposed)} tables without row-level security ({names}), so "
                   "Supabase's web API can reach them. Re-apply the migrations (sql/999_lock_public.sql).")
        else:
            record("api lockdown", PASS, "row-level security on every table; Supabase's web API sees nothing")

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

        # Name-based, not a count: migrations add jobs over time and a total
        # would go stale every phase.
        required = {"morning_brief", "pre_shift", "post_shift", "wrap",
                    "reflection", "entity_sync", "heartbeat"}
        names = {j["name"] for j in await store.jobs()}
        missing = required - names
        record(
            "jobs",
            PASS if not missing else FAIL,
            f"{len(names)} seeded"
            if not missing
            else f"missing {sorted(missing)}. Re-apply the sql/ migrations.",
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
    if name == "local" and not (config.local_base_url and config.local_model):
        record(label, FAIL, "LOCAL_BASE_URL and LOCAL_MODEL are unset (LOCAL_MODELS.md)")
        return

    provider = build(name, config, bulk=bulk)
    try:
        completion = await provider.complete("Reply with the single word: ok", "ok", max_tokens=512)
    except ProviderError as exc:
        record(label, FAIL, f"{name} is configured but the call failed: {exc.message}")
        return
    record(label, PASS, f"{name} answered ({completion.usage.model or 'model unreported'})")


async def check_fallbacks(config: Settings) -> None:
    """Check the providers the router would degrade *to*, not just the two set.

    A fallback chain is only as good as its key wiring, and the usual way that
    is discovered is a scheduled job failing at 6:35 AM with every provider in
    the lane broken at once. Better to know now. These are warnings, not
    failures: a configured provider that works is enough to run.
    """
    configured = {config.main_provider, config.bulk_provider}
    spares = [n for n in MAIN_ORDER if n not in configured]
    if not spares:
        record("fallbacks", PASS, "every provider is already a configured lane")
        return

    for name in spares:
        provider = build(name, config, bulk=False)
        if not provider.configured:
            record(f"fallback: {name}", SKIP, "not set up (optional; LOCAL_MODELS.md)")
            continue
        try:
            await provider.complete("Reply with the single word: ok", "ok", max_tokens=512)
        except ProviderError as exc:
            record(
                f"fallback: {name}",
                WARN,
                f"unavailable, so the router cannot degrade to it: {exc.message}",
            )
            continue
        record(f"fallback: {name}", PASS, "ready to take over")


async def check_school(config: Settings) -> None:
    """Canvas and the calendar feed. Both read-only, both credentials."""
    from sloane.school import SchoolError

    if (not config.canvas_token or not config.canvas_base_url) and config.canvas_feed_url:
        from datetime import datetime, timedelta, timezone

        from sloane.school.calendar import fetch as fetch_ics
        from sloane.school.canvas_feed import parse_feed

        try:
            now = datetime.now(timezone.utc)
            items = parse_feed(await fetch_ics(config.canvas_feed_url, what="Canvas feed"),
                               window_start=now - timedelta(days=config.sync_past_days),
                               window_end=now + timedelta(days=config.sync_future_days), tz=config.timezone)
        except SchoolError as exc:
            record("canvas", FAIL, str(exc))
        else:
            record("canvas", PASS, f"{len(items)} assignments from the Calendar Feed "
                   "(due dates only: a token adds grades and turned-in state)")
    elif not config.canvas_token or not config.canvas_base_url:
        record(
            "canvas",
            WARN,
            "CANVAS_BASE_URL/CANVAS_TOKEN unset, so assignments will not sync. "
            "Canvas → Account → Settings → New Access Token, or, if that button isn't there, "
            "CANVAS_FEED_URL from Canvas → Calendar → Calendar Feed.",
        )
    else:
        from sloane.school.canvas import CanvasClient

        try:
            courses = await CanvasClient(
                config.canvas_base_url, config.canvas_token
            ).courses()
        except SchoolError as exc:
            record("canvas", FAIL, str(exc))
        else:
            record("canvas", PASS, f"{len(courses)} active courses, read-only")

    if not config.calendar_ics_url:
        record(
            "calendar",
            WARN,
            "CALENDAR_ICS_URL unset. Google Calendar → Settings for my calendars "
            "→ Integrate calendar → Secret address in iCal format.",
        )
        return

    from datetime import datetime, timedelta, timezone

    from sloane.school.calendar import fetch as fetch_ics, parse as parse_ics

    try:
        body = await fetch_ics(config.calendar_ics_url)
        now = datetime.now(timezone.utc)
        events = parse_ics(
            body,
            window_start=now - timedelta(days=config.sync_past_days),
            window_end=now + timedelta(days=config.sync_future_days),
            tz=config.timezone,
        )
    except SchoolError as exc:
        # The URL is a credential; report the failure, never the URL.
        record("calendar", FAIL, str(exc))
        return
    record("calendar", PASS, f"{len(events)} events in the sync window")


async def check_gmail(config: Settings) -> None:
    """Refresh the token and read the profile. Proves the grant is alive."""
    from sloane.mail import MailError
    from sloane.mail.gmail import GmailClient

    gmail = GmailClient(config)
    if not gmail.configured:
        record(
            "gmail",
            WARN,
            "GMAIL_CLIENT_ID/SECRET/REFRESH_TOKEN unset, so no inbox triage. "
            "See DEPLOY.md → Gmail, then run scripts/gmail_auth.py once.",
        )
        return
    try:
        profile = await gmail.profile()
    except MailError as exc:
        record("gmail", FAIL, str(exc))
        return
    domains = ", ".join(config.school_domains) or "none"
    record(
        "gmail", PASS,
        f"authorised for {profile.get('emailAddress', '?')}; school domains: {domains}",
    )


async def check_voice(config: Settings, *, warm: bool) -> None:
    """P3. Voice is optional -- every failure here still leaves text replies."""
    import shutil
    import subprocess

    ffmpeg = shutil.which(config.ffmpeg_bin)
    if ffmpeg is None:
        record("voice: ffmpeg", WARN, f"{config.ffmpeg_bin} not found, so replies stay text. apt-get install ffmpeg.")
    else:
        encoders = subprocess.run([ffmpeg, "-hide_banner", "-encoders"],
                                  capture_output=True, text=True).stdout
        if "libopus" in encoders:
            record("voice: ffmpeg", PASS, "can encode Opus voice notes")
        else:
            record("voice: ffmpeg", WARN, "this ffmpeg lacks libopus; voice notes will fail over to text")

    import importlib.util

    from sloane.providers.tts import PiperTTS

    local = PiperTTS(config)
    has_piper = importlib.util.find_spec("piper") is not None or bool(shutil.which(config.piper_bin))
    usable = local.path is not None and (local.path.is_file() or local.by_name)
    lanes = {"groq": bool(config.groq_api_key), "piper": usable and has_piper}
    if local.path is not None:
        if local.path.is_file():
            record("voice: piper", PASS, f"{local.path.name} is on disk")
        elif local.by_name:
            record("voice: piper", WARN, f"{config.piper_voice} will be fetched at startup into {local.path.parent}")
        else:
            record("voice: piper", FAIL, f"PIPER_VOICE={config.piper_voice} is not a file; use a voice name "
                   "like en_GB-cori-medium or a path to an .onnx")
    ready = [n for n, ok in lanes.items() if ok]
    if not ready:
        record("voice: tts", WARN, "no speech provider ready (GROQ_API_KEY, or PIPER_VOICE); replies stay text")
        return
    if not warm:
        record("voice: tts", PASS, f"configured: {', '.join(ready)} (live check with --warm)")
        return
    from sloane.router import NoProviderAvailable, Router

    try:
        audio = await Router(config).speak("Sloane voice check.")
    except NoProviderAvailable as exc:
        record("voice: tts", FAIL, str(exc))
        return
    record("voice: tts", PASS, f"{audio.usage.provider} spoke, {len(audio.wav)} bytes of audio")


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


def check_values(config: Settings) -> None:
    """The installer's paste checks, over whatever .env holds now (hand edits
    included): the precise fix for a value that can't work, before the
    connection checks below fail on it less helpfully."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from env_check import problem

    wrong = []
    for key in ("DATABASE_URL", "GROQ_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
                "CANVAS_BASE_URL", "CANVAS_TOKEN", "CANVAS_FEED_URL", "CALENDAR_ICS_URL", "LOCAL_BASE_URL",
                "GITHUB_TOKEN"):
        value = getattr(config, key.lower())
        if not value:
            continue  # unset is each check's own business below
        # As the app will use it: the installer's cleaning never ran on a hand edit.
        issue = problem(key, str(value).strip())
        if issue:
            wrong.append(f"{key}: {issue}")
    if wrong:
        record("settings", FAIL, " | ".join(wrong) + " (nano .env, then restart)")
    else:
        record("settings", PASS, "every value is the right shape")


def check_timezone(config: Settings) -> None:
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(config.timezone)
    except Exception as exc:  # noqa: BLE001
        record("timezone", FAIL, f"{config.timezone!r} is not a zone: {exc}")
        return
    record("timezone", PASS, config.timezone)


def check_extras(config: Settings) -> None:
    """The post-v1 settings: Capture, backups and voice briefs."""
    from pathlib import Path

    from sloane.capture import MIN_TOKEN
    from sloane.jobs.briefs import SPEAKING

    token = config.capture_token or ""
    if not token:
        record("capture", SKIP, "CAPTURE_TOKEN unset, so POST /capture is off (DEPLOY §7d)")
    elif len(token) < MIN_TOKEN:
        record("capture", FAIL, f"CAPTURE_TOKEN is under {MIN_TOKEN} characters, so capture stays off")
    else:
        record("capture", PASS, "POST /capture is on (reach it over Tailscale, never a public port)")
        if not config.cors_origins.strip():
            record("capture: cors", WARN, "CORS_ORIGINS is empty, so the Capture app (a web page) can't reach "
                   "/capture; add its address, e.g. CORS_ORIGINS=https://<your-capture-app>.vercel.app (DEPLOY §7d)")
        else:
            record("capture: cors", PASS, f"/capture answers {config.cors_origins.strip()}")

    from sloane import web

    if not config.dashboard_token:
        record("control room", SKIP, "DASHBOARD_TOKEN unset, so /app is off (DEPLOY §7e; the installer makes one)")
    elif not web.enabled(config):
        record("control room", FAIL, f"DASHBOARD_TOKEN is under {web.MIN_TOKEN} characters, so /app stays off")
    else:
        record("control room", PASS, "/app is on (open it over Tailscale: https://<box>.<tailnet>.ts.net/app)")

    folder = config.backup_dir or (str(Path(config.embed_cache_dir) / "backups")
                                   if config.embed_cache_dir else "")
    if not folder:
        record("backups", WARN, "no BACKUP_DIR or EMBED_CACHE_DIR: nightly backups have nowhere to go")
    else:
        try:
            Path(folder).mkdir(parents=True, exist_ok=True)
            probe = Path(folder) / ".doctor"
            probe.write_text("ok")
            probe.unlink()
            record("backups", PASS, f"nightly JSON to {folder}, {config.backup_keep} kept")
        except OSError as exc:
            record("backups", FAIL, f"{folder} is not writable: {exc}")

    unknown = sorted(config.voice_brief_names - SPEAKING - {"weekly_review"})
    if unknown:
        record("voice briefs", WARN, f"VOICE_BRIEFS names no such brief: {', '.join(unknown)} "
               f"(choose from {', '.join(sorted(SPEAKING | {'weekly_review'}))})")
    elif config.voice_brief_names:
        record("voice briefs", PASS, ", ".join(sorted(config.voice_brief_names)))


async def check_skills(config: Settings) -> None:
    """Which skills load, and whether their settings are usable."""
    import pkgutil
    import re

    import sloane.skills as package
    from sloane.skills import SkillContext, load
    from sloane.skills.weather import WeatherUnavailable, parse_location

    registry = load(SkillContext(store=None, config=config))
    record("skills", PASS, ", ".join(registry.names) or "none loaded")
    known = {m.name for m in pkgutil.iter_modules(package.__path__) if not m.name.startswith("_")}
    unknown = sorted(config.disabled_skills - known - set(registry.names))
    if unknown:
        record("skills disabled", WARN, f"SKILLS_DISABLED names no such skill: {', '.join(unknown)}")

    if not config.weather_location.strip():
        record("weather", SKIP, "WEATHER_LOCATION unset, so there is no weather (e.g. 39.52,-104.76 for Parker)")
    elif parse_location(config.weather_location) is None:
        record("weather", FAIL, f"WEATHER_LOCATION {config.weather_location!r} is not 'latitude,longitude'")
    else:
        weather = registry.get("weather")
        try:
            forecast = await weather.forecast() if weather is not None else None
        except WeatherUnavailable as exc:
            record("weather", WARN, f"Open-Meteo did not answer: {exc}")
        else:
            now = "" if forecast is None or forecast.temp is None else f", {round(forecast.temp)}° now"
            record("weather", PASS, f"Open-Meteo answered{now}")

    bad = [name for name in ("plan_school_day_start", "plan_weekend_start", "plan_bedtime")
           if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", getattr(config, name))]
    if bad:
        record("plan", FAIL, f"{', '.join(n.upper() for n in bad)} must be HH:MM")
    if config.pay_rate < 0:
        record("pay rate", FAIL, "PAY_RATE can't be negative")
    elif config.pay_rate:
        record("pay rate", PASS, f"${config.pay_rate:g}/h for earnings estimates")


async def check_local(config: Settings) -> None:
    """A local model, if one is set up: reachable, serving LOCAL_MODEL, and how fast."""
    from sloane.providers.local import LocalProvider

    local = LocalProvider(config)
    if not local.configured:
        record("local model", SKIP, "none set up (optional; LOCAL_MODELS.md)")
        return
    try:
        served = await local.models()
    except ProviderError as exc:
        record("local model", FAIL, f"{exc.message} -- is the server up, and listening beyond localhost?")
        return
    names = {m.removesuffix(":latest") for m in served}
    if config.local_model.removesuffix(":latest") not in names:
        record("local model", FAIL, f"{config.local_model!r} isn't on that server "
                                    f"(it has: {', '.join(sorted(names)[:6]) or 'nothing'}). "
                                    f"On it: ollama pull {config.local_model}")
        return
    try:
        answer = await local.complete("Reply with the single word: ok", "ok", max_tokens=16)
    except ProviderError as exc:
        record("local model", FAIL, f"listed but the call failed: {exc.message}")
        return
    seconds = (answer.usage.latency_ms or 0) / 1000
    note = " -- slow for chat; fine for BULK_PROVIDER=local" if seconds > 15 else ""
    record("local model", PASS, f"{config.local_model} answered in {seconds:.1f}s{note}")


async def check_workshop(config: Settings) -> None:
    """The workshop: a token that can push to her repo, git, and the box's upgrader."""
    import httpx

    if not config.github_token:
        record("workshop", SKIP, "GITHUB_TOKEN unset: ideas and plans work, building doesn't (DEPLOY §7h)")
        return
    if shutil.which("git") is None:
        record("workshop", FAIL, "git isn't in the image; rebuild it (rerun the installer)")
        return
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(f"{config.github_api_base.rstrip('/')}/repos/{config.github_repo}",
                                        headers={"Authorization": f"Bearer {config.github_token}",
                                                 "Accept": "application/vnd.github+json"})
    except httpx.HTTPError as exc:
        record("workshop", FAIL, f"can't reach GitHub: {exc}")
        return
    if response.status_code == 401:
        record("workshop", FAIL, "GitHub refused the token (expired or mistyped?); make a new one (DEPLOY §7h)")
        return
    if response.status_code == 404:
        record("workshop", FAIL, f"the token can't see {config.github_repo}: pick it under Repository access (DEPLOY §7h)")
        return
    if response.status_code >= 300:
        record("workshop", FAIL, f"GitHub answered {response.status_code}")
        return
    if not (response.json().get("permissions") or {}).get("push"):
        record("workshop", FAIL, "the token can read but not write: Contents and Pull requests need Read and write")
        return
    deploy = Path(config.deploy_dir)
    if not (deploy / "host.json").is_file():
        record("workshop", WARN, "builds work; accepted changes merge but go live only when you rerun the installer "
                                 "(it installs the automatic upgrade)")
        return
    record("workshop", PASS, f"builds on {config.workshop_model}; accepted changes go live on their own")


async def main(warm: bool = False) -> int:
    config = load_settings()

    print(f"Sloane doctor — main={config.main_provider} bulk={config.bulk_provider}\n")

    check_values(config)
    check_timezone(config)
    await check_database(config)
    check_embedder(config, warm=warm)
    await check_provider(config, config.main_provider, "main provider", bulk=False)
    await check_provider(config, config.bulk_provider, "bulk provider", bulk=True)
    await check_fallbacks(config)
    await check_local(config)
    await check_school(config)
    await check_gmail(config)
    await check_voice(config, warm=warm)
    await check_telegram(config)
    check_extras(config)
    await check_workshop(config)
    await check_skills(config)

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
