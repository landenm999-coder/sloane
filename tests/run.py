#!/usr/bin/env python3
"""Run every suite. Integration tests are skipped unless DATABASE_URL is set."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

UNIT = [
    "test_invariants.py",
    "test_contract.py",
    "test_router.py",
    "test_tiers.py",
    "test_embed.py",
    "test_matching.py",
    "test_conflicts.py",
    "test_agent.py",
    "test_voice.py",
    "test_views.py",
    "test_changes.py",
    "test_skills.py",  # its integration half runs when DATABASE_URL is set
    "test_cors.py",
    "test_dates.py",
    "test_weather.py",
    "test_dashboard.py",  # its integration half runs when DATABASE_URL is set
    "test_conversation.py",  # likewise
    "test_claude_stream.py",
    "test_live.py",  # integration only
    "test_actions.py",
]
INTEGRATION = [
    "test_store.py", "test_school.py", "test_jobs.py", "test_agency.py", "test_mail.py",
    "test_reminders.py", "test_watchdog.py", "test_capture.py",
    "test_promises.py", "test_weekly.py", "test_heartbeat.py", "test_lists.py",
    "test_countdowns.py", "test_cards.py", "test_habits.py", "test_clients.py", "test_plan.py", "test_focus.py", "test_birthdays.py", "test_money.py",
]


def run(name: str) -> bool:
    proc = subprocess.run(
        [sys.executable, str(HERE / name)],
        capture_output=True,
        text=True,
    )
    ok = proc.returncode == 0
    print(f"  [{'ok  ' if ok else 'FAIL'}] {name}")
    if not ok:
        for line in (proc.stdout + proc.stderr).strip().splitlines():
            print(f"         {line}")
    return ok


def main() -> int:
    print("unit")
    results = [run(name) for name in UNIT]

    if os.environ.get("DATABASE_URL"):
        print("integration")
        results += [run(name) for name in INTEGRATION]
    else:
        print("integration")
        print("  [skip] set DATABASE_URL to run test_store.py against a live Postgres")

    failed = results.count(False)
    print()
    print(f"{len(results) - failed}/{len(results)} suites passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
