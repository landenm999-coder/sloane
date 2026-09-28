#!/usr/bin/env python3
"""Catch a bad paste before it becomes a broken .env.

The installer runs every answer through here and asks again, with the reason,
when one is wrong; doctor runs the same checks over an existing .env. Only the
standard library, because the installer runs it on the bare host.

    SLOANE_VALUE=... python3 scripts/env_check.py DATABASE_URL

prints the cleaned value and exits 0, or prints what is wrong to stderr and
exits 1. The value comes in through the environment, never argv (argv is
world-readable in `ps`), and a problem never repeats the value.
"""

from __future__ import annotations

import os
import re
import sys
from urllib.parse import urlsplit

# Blank is fine for these: she runs without them and says so in /status.
OPTIONAL = {"GROQ_API_KEY", "CANVAS_BASE_URL", "CANVAS_TOKEN", "CANVAS_FEED_URL", "CALENDAR_ICS_URL",
            "LOCAL_BASE_URL", "GITHUB_TOKEN"}

_BAD_PASSWORD = re.compile(r"[@#/?\[\]\s$]|%(?![0-9A-Fa-f]{2})")
_RESET = ("Easiest fix: Supabase → Project Settings → Database → Reset database password, "
          "pick letters and numbers only, and paste the string again.")


def clean(key: str, value: str) -> str:
    """Whitespace and wrapping quotes off, and the forms people paste made usable."""
    value = (value or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1].strip()
    if key == "TELEGRAM_BOT_TOKEN":
        value = value.removeprefix("bot")
    if key == "CANVAS_BASE_URL" and value:
        if "://" not in value:
            value = "https://" + value
        parts = urlsplit(value)
        value = f"{parts.scheme}://{parts.netloc}" if parts.netloc else value
    if key in ("CALENDAR_ICS_URL", "CANVAS_FEED_URL") and value.startswith("webcal://"):
        value = "https://" + value.removeprefix("webcal://")
    return value


def _database(value: str) -> str | None:
    if "YOUR-PASSWORD" in value.upper() or "[PASSWORD]" in value.upper():
        return "It still says [YOUR-PASSWORD]. Put your database password there, without the brackets."
    if not value.startswith(("postgresql://", "postgres://")):
        return "It should start with postgresql:// (Supabase → Connect → Session pooler)."
    rest = value.split("://", 1)[1]
    userinfo, at, hostpart = rest.rpartition("@")
    if not at:
        if "supabase" in value:
            return "There's no password in it. Use the whole string from Supabase, with your password in it."
        return None  # a local socket database: nothing to check
    password = userinfo.partition(":")[2]
    if password.startswith("[") and password.endswith("]"):
        return "Take the square brackets off your password."
    if _BAD_PASSWORD.search(password) or "@" in userinfo.partition(":")[0]:
        return "Your database password has a symbol (like @ # / ? $ or a space) that breaks the address. " + _RESET
    host = hostpart.split("/", 1)[0]
    name, _, port = host.rpartition(":") if ":" in host else (host, "", "")
    if port == "6543":
        return "That's the Transaction pooler (port 6543). Use the Session pooler string, port 5432."
    if port and not port.isdigit():
        return "The port after the host isn't a number. Paste the Session pooler string again."
    if re.fullmatch(r"db\.[a-z0-9]+\.supabase\.co", name or host):
        return ("That's the Direct connection, which most servers can't reach (it's IPv6 only). "
                "Use the Session pooler string: its host ends in pooler.supabase.com.")
    return None


def _calendar(value: str) -> str | None:
    if not value.startswith("https://"):
        return "It should start with https:// (the Secret address in iCal format)."
    if "calendar.google.com" in value:
        path = urlsplit(value).path
        if not path.endswith(".ics"):
            return ("That's the calendar's web page. Use Settings → your calendar → Integrate calendar → "
                    "Secret address in iCal format (it ends in basic.ics).")
        if "/public/" in path:
            return "That's the Public address. Use the Secret address in iCal format (it has 'private' in it)."
    return None


def problem(key: str, value: str) -> str | None:
    """What is wrong with a cleaned value, in words he can act on; None if it looks right."""
    if not value:
        return None if key in OPTIONAL else "This one is required."
    if key == "DATABASE_URL":
        return _database(value)
    if key == "GROQ_API_KEY" and (not value.startswith("gsk_") or re.search(r"\s", value)):
        return "Groq keys start with gsk_ (console.groq.com/keys → Create API key)."
    if key == "TELEGRAM_BOT_TOKEN" and not re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{30,}", value):
        return "A bot token looks like 123456789:AAH… (the whole line @BotFather sent)."
    if key == "TELEGRAM_CHAT_ID" and not re.fullmatch(r"-?\d{3,}", value):
        return "Just the number (like 123456789), from @userinfobot. Not your @username."
    if key == "CANVAS_BASE_URL":
        parts = urlsplit(value)
        if parts.scheme != "https" or "." not in (parts.hostname or ""):
            return "The address you open Canvas at, like https://dcsd.instructure.com."
        if parts.path.strip("/") or parts.query:
            return "Just the address, like https://dcsd.instructure.com, with nothing after it."
    if key == "CANVAS_TOKEN" and (len(value) < 20 or re.search(r"\s", value)):
        return "That's too short for a Canvas token. Canvas → Account → Settings → + New Access Token, copy all of it."
    if key == "CANVAS_FEED_URL" and (not value.startswith("https://") or "/feeds/calendars/" not in value):
        return "Canvas → Calendar → Calendar Feed (bottom right) → copy the whole link. It has /feeds/calendars/ in it."
    if key == "CALENDAR_ICS_URL":
        return _calendar(value)
    if key == "LOCAL_BASE_URL":
        return _local(value)
    if key == "GITHUB_TOKEN" and (not re.match(r"^(github_pat_|ghp_)[A-Za-z0-9_]{20,}$", value)):
        return ("A GitHub token starts with github_pat_ (GitHub → Settings → Developer settings → Fine-grained "
                "tokens → Generate new token; DEPLOY 7h has the exact boxes to tick).")
    return None


def _local(value: str) -> str | None:
    """A local model server's OpenAI address, as seen from inside her container."""
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "The model server's address, like http://raspberrypi.local:11434/v1 (Ollama)."
    if parts.hostname in ("localhost", "127.0.0.1", "::1"):
        return ("Inside her container, localhost is the container itself. Ollama on this same box is "
                "http://host.docker.internal:11434/v1; on another machine, use its name or Tailscale address.")
    if not parts.path.rstrip("/").endswith("/v1"):
        return "It needs the OpenAI path on the end: …:11434/v1 for Ollama, …:8080/v1 for llama.cpp."
    return None


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: SLOANE_VALUE=... env_check.py KEY", file=sys.stderr)
        return 2
    key = argv[1]
    value = clean(key, os.environ.get("SLOANE_VALUE", ""))
    wrong = problem(key, value)
    if wrong:
        print(f"  {wrong}", file=sys.stderr)
        return 1
    print(value, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
