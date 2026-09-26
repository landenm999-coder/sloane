"""The installer's paste checks: each common wrong paste gets its reason, the
right ones pass, and no reason ever repeats the value."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from env_check import clean, problem

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{label}\n     got: {got!r}\n    want: {want!r}")


POOLER = "postgresql://postgres.abcdefghij:Hunter2secret@aws-0-us-west-1.pooler.supabase.com:5432/postgres"
TOKEN = "7123456789:AAHfq2x_wQ-9vR3kLmN0pZyXcVbT8sJdE4u"
ICS = "https://calendar.google.com/calendar/ical/landen%40gmail.com/private-0a1b2c3d4e5f/basic.ics"

GOOD = [
    ("DATABASE_URL", POOLER),
    ("DATABASE_URL", "postgresql://postgres@/sloane?host=/tmp&port=5433"),  # local dev
    ("DATABASE_URL", "postgresql://postgres.abc:p%40ss@aws-0-us-west-1.pooler.supabase.com:5432/postgres"),
    ("GROQ_API_KEY", "gsk_" + "a" * 52),
    ("GROQ_API_KEY", ""),
    ("TELEGRAM_BOT_TOKEN", TOKEN),
    ("TELEGRAM_CHAT_ID", "123456789"),
    ("TELEGRAM_CHAT_ID", "-1001234567890"),
    ("CANVAS_BASE_URL", "https://dcsd.instructure.com"),
    ("CANVAS_TOKEN", "1234~" + "x" * 64),
    ("CALENDAR_ICS_URL", ICS),
    ("CALENDAR_ICS_URL", ""),
    ("CANVAS_FEED_URL", "https://dcsd.instructure.com/feeds/calendars/user_AbC123xyz.ics"),
    ("CANVAS_FEED_URL", ""),
]
for key, value in GOOD:
    check(f"{key} passes: {value[:40]}", problem(key, clean(key, value)), None)

BAD = [
    ("the placeholder left in", "DATABASE_URL",
     "postgresql://postgres.abc:[YOUR-PASSWORD]@aws-0-us-west-1.pooler.supabase.com:5432/postgres", "[YOUR-PASSWORD]"),
    ("brackets kept round the password", "DATABASE_URL",
     "postgresql://postgres.abc:[Hunter2]@aws-0-us-west-1.pooler.supabase.com:5432/postgres", "square brackets"),
    ("the transaction pooler", "DATABASE_URL", POOLER.replace(":5432/", ":6543/"), "6543"),
    ("the direct connection", "DATABASE_URL",
     "postgresql://postgres:Hunter2@db.abcdefghij.supabase.co:5432/postgres", "IPv6"),
    ("an @ in the password", "DATABASE_URL",
     "postgresql://postgres.abc:p@ss@aws-0-us-west-1.pooler.supabase.com:5432/postgres", "symbol"),
    ("a # in the password", "DATABASE_URL",
     "postgresql://postgres.abc:pa#ss@aws-0-us-west-1.pooler.supabase.com:5432/postgres", "symbol"),
    ("a $ in the password (compose reads it as a variable)", "DATABASE_URL",
     "postgresql://postgres.abc:pa$ss@aws-0-us-west-1.pooler.supabase.com:5432/postgres", "symbol"),
    ("not a URL at all", "DATABASE_URL", "Hunter2", "postgresql://"),
    ("required", "DATABASE_URL", "", "required"),
    ("a Groq key from somewhere else", "GROQ_API_KEY", "sk-ant-abc123", "gsk_"),
    ("half a bot token", "TELEGRAM_BOT_TOKEN", "7123456789", "BotFather"),
    ("a username for the chat id", "TELEGRAM_CHAT_ID", "@landen", "Not your @username"),
    ("a login page for Canvas", "CANVAS_BASE_URL", "http://dcsd.instructure.com", "https://"),
    ("a Canvas page, not the address (a hand edit)", "CANVAS_BASE_URL",
     "https://dcsd.instructure.com/courses/123", "nothing after"),
    ("a truncated Canvas token", "CANVAS_TOKEN", "1234~abc", "too short"),
    ("the Canvas calendar page, not its feed", "CANVAS_FEED_URL",
     "https://dcsd.instructure.com/calendar", "Calendar Feed"),
    ("the calendar's web page", "CALENDAR_ICS_URL",
     "https://calendar.google.com/calendar/u/0/r", "Secret address"),
    ("the public iCal address", "CALENDAR_ICS_URL",
     "https://calendar.google.com/calendar/ical/landen%40gmail.com/public/basic.ics", "Public address"),
]
for label, key, value, words in BAD:
    got = problem(key, value) or ""
    check(f"{label}: says why", words.lower() in got.lower(), True)
    secret = value.split(":")[-1] if key == "DATABASE_URL" and "@" in value else value
    if len(secret) > 8:
        check(f"{label}: never repeats the value", secret in got, False)

check("pasted with quotes and spaces", clean("GROQ_API_KEY", '  "gsk_abc"  '), "gsk_abc")
check("a token pasted from a URL", clean("TELEGRAM_BOT_TOKEN", "bot" + TOKEN), TOKEN)
check("Canvas: the page he was on is cut to the address",
      clean("CANVAS_BASE_URL", "https://dcsd.instructure.com/courses/123?x=1"), "https://dcsd.instructure.com")
check("Canvas: the scheme is added", clean("CANVAS_BASE_URL", "dcsd.instructure.com"), "https://dcsd.instructure.com")
check("webcal is https", clean("CALENDAR_ICS_URL", ICS.replace("https://", "webcal://")), ICS)
check("webcal is https for the Canvas feed too",
      clean("CANVAS_FEED_URL", "webcal://dcsd.instructure.com/feeds/calendars/user_x.ics"),
      "https://dcsd.instructure.com/feeds/calendars/user_x.ics")

# The CLI the installer calls: the value in through the environment, out on stdout.
def cli(key: str, value: str) -> tuple[int, str, str]:
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "env_check.py"), key],
                          env={**os.environ, "SLOANE_VALUE": value}, capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


check("CLI: a good value comes back cleaned", cli("CANVAS_BASE_URL", " dcsd.instructure.com/ ")[:2],
      (0, "https://dcsd.instructure.com"))
code, out, err = cli("DATABASE_URL", POOLER.replace(":5432/", ":6543/"))
check("CLI: a bad one fails with the reason, and nothing on stdout", (code, out, "6543" in err), (1, "", True))

if FAILURES:
    print(f"FAIL ({len(FAILURES)})")
    for f in FAILURES:
        print("  -", f)
    raise SystemExit(1)
print("env_check: bad pastes caught with the reason, good ones pass, values never echoed")
