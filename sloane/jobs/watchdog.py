"""Tell Landen when something breaks, once, and again when it's fixed.

Sloane runs on a box he never logs into. Without this, an expired Claude login
or a revoked Gmail grant would show up as briefs that quietly stopped coming,
and he would stop trusting all of it. So every 30 minutes, with no model call:

    job:<name>        a job's last run failed or only partly worked
    provider:<name>   the main or bulk model failed every call for 3 hours
    gmail             Google says the Gmail grant is revoked or expired

A problem is told once it has lasted GRACE (a Canvas timeout that clears on
the next sync is not news), repeated daily while it lasts, and followed by a
"fixed" line when it clears. Quiet hours hold the whole check till morning.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from sloane.config import Settings

Row = dict[str, Any]

GRACE = timedelta(minutes=60)
REPEAT = timedelta(hours=24)
PROVIDER_WINDOW_HOURS = 3
PROVIDER_MIN_FAILURES = 3

# What to do about each, in words he can act on.
HINTS = {
    "claude_code": "the Claude CLI login has probably expired: "
                   "docker compose run --rm sloane claude (DEPLOY §6)",
    "groq": "check GROQ_API_KEY, or Groq's free-tier limit for today",
    "anthropic": "check ANTHROPIC_API_KEY and the account's credit",
}


def problems(jobs: Sequence[Row], health: Sequence[Row], config: Settings) -> dict[str, str]:
    """Every problem present right now, as key -> message. Pure."""
    found: dict[str, str] = {}
    for job in jobs:
        name, status, error = job["name"], job.get("last_status"), (job.get("last_error") or "")
        if name == "watchdog" or not job.get("enabled", True):
            continue
        if "gmail_auth.py" in error:
            found["gmail"] = ("Gmail access has stopped working: run python3 scripts/gmail_auth.py "
                              "on the box (and check the Google app is In production, not Testing).")
            continue
        if status == "failed":
            found[f"job:{name}"] = f"The {name} job is failing: {error[:160] or 'no detail'}"
        elif status == "partial":
            found[f"job:{name}"] = f"The {name} job only partly worked: {error[:160] or 'no detail'}"

    lanes = {config.main_provider: "main", config.bulk_provider: "bulk"}
    for row in health:
        provider = row["provider"]
        if provider not in lanes:
            continue
        calls, failures = int(row["calls"]), int(row["failures"])
        if failures >= PROVIDER_MIN_FAILURES and failures == calls:
            hint = HINTS.get(provider, "check its credentials")
            found[f"provider:{provider}"] = (
                f"Every {lanes[provider]}-lane call to {provider} has failed for "
                f"{PROVIDER_WINDOW_HOURS} hours; {hint}."
            )
    return found


def recovered(key: str) -> str:
    kind, _, name = key.partition(":")
    if kind == "job":
        return f"The {name} job is working again."
    if kind == "provider":
        return f"{name} is answering again."
    if key == "gmail":
        return "Gmail access is working again."
    return f"Resolved: {key}."


def plan(open_rows: Sequence[Row], current: dict[str, str], now: datetime) -> tuple[list[str], list[str]]:
    """(lines to send, keys whose rows are now notified). Pure.

    `open_rows` are unresolved alerts *after* this run's sightings were recorded.
    """
    lines: list[str] = []
    notified: list[str] = []
    for row in open_rows:
        key = row["key"]
        if key not in current:
            continue
        if row.get("notified_at") is None:
            if now - row["first_seen"] >= GRACE:
                lines.append(f"⚠️ {current[key]}")
                notified.append(key)
        elif now - row["notified_at"] >= REPEAT:
            lines.append(f"⚠️ Still: {current[key]}")
            notified.append(key)
    return lines, notified


async def run(store, config: Settings, say, now: datetime) -> tuple[int, int]:  # noqa: ANN001
    """One pass. Returns (problems present, lines sent)."""
    current = problems(await store.jobs(), await store.provider_health(PROVIDER_WINDOW_HOURS), config)

    fixed: list[str] = []
    for row in await store.open_alerts():
        if row["key"] not in current:
            await store.resolve_alert(row["key"], now)
            if row.get("notified_at") is not None:
                fixed.append(f"✅ {recovered(row['key'])}")
    for key, message in current.items():
        await store.see_alert(key, message, now)

    lines, notified = plan(await store.open_alerts(), current, now)
    lines.extend(fixed)
    if not lines:
        return len(current), 0
    await say("\n".join(lines))
    for key in notified:
        await store.mark_alert_notified(key, now)
    return len(current), len(lines)
