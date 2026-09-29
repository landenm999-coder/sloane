#!/usr/bin/env python3
"""Run every suite. Integration tests are skipped unless DATABASE_URL is set."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
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
    "test_coin.py",
    "test_dashboard.py",  # its integration half runs when DATABASE_URL is set
    "test_conversation.py",  # likewise
    "test_claude_stream.py",
    "test_live.py",  # integration only
    "test_actions.py",
    "test_lookup.py",
    "test_learn.py",  # its integration half runs when DATABASE_URL is set
    "test_piper.py",
    "test_format.py",
    "test_env_check.py",
    "test_canvas_feed.py",  # its integration half runs when DATABASE_URL is set
    "test_forward.py",  # likewise
    "test_local.py",
    "test_web.py",  # its integration half runs when DATABASE_URL is set
    "test_workshop.py",  # likewise
    "test_markets.py",  # likewise
    "test_news.py",
    "test_whoop.py",
]
INTEGRATION = [
    "test_store.py", "test_school.py", "test_jobs.py", "test_agency.py", "test_mail.py",
    "test_reminders.py", "test_watchdog.py", "test_capture.py",
    "test_promises.py", "test_weekly.py", "test_heartbeat.py", "test_lists.py",
    "test_countdowns.py", "test_cards.py", "test_habits.py", "test_clients.py", "test_plan.py", "test_focus.py", "test_birthdays.py", "test_money.py",
    "test_colleges.py", "test_deca.py", "test_workouts.py", "test_recent.py", "test_hello.py", "test_boot.py",
]


# No suite takes more than a minute or two. One that runs past this is stuck: it is
# named, every thread's stack is printed (faulthandler, on SIGABRT), and it fails,
# instead of silently eating CI's twenty minutes (as one did on main after #15).
SUITE_SECONDS = int(os.environ.get("SUITE_SECONDS", "300"))


def run(name: str) -> bool:
    started = time.monotonic()
    proc = subprocess.Popen(
        [sys.executable, "-u", "-X", "faulthandler", str(HERE / name)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        output, _ = proc.communicate(timeout=SUITE_SECONDS)
        ok = proc.returncode == 0
        label = "ok  " if ok else "FAIL"
    except subprocess.TimeoutExpired:
        proc.send_signal(signal.SIGABRT)
        try:
            output, _ = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            output, _ = proc.communicate()
        ok, label = False, "STUCK"
        output = (output or "") + f"\n(still running after {SUITE_SECONDS} s: stopped, with every thread's stack above)"
    print(f"  [{label}] {name} ({time.monotonic() - started:.0f} s)", flush=True)
    if not ok:
        for line in (output or "").strip().splitlines()[-80:]:
            print(f"         {line}", flush=True)
    return ok


def main() -> int:
    print("unit", flush=True)
    results = [run(name) for name in UNIT]

    if os.environ.get("DATABASE_URL"):
        print("integration", flush=True)
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
