"""Secrets out of text: cards, keys, passwords, PINs, SSNs, logins in URLs; ordinary numbers left alone.
Through the outside-text helpers, the log scrubber, and (with DATABASE_URL) the conversation log.

DESTRUCTIVE (integration half): deletes its own rows from messages.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _settings import isolated

from sloane.ingest import safe_field, unfence
from sloane.redact import LogScrubber, redact, removed

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{label}\n     got: {got!r}\n    want: {want!r}")


# -- what goes ------------------------------------------------------------------------------------
check("a card number, spaced", redact("my card is 4111 1111 1111 1111 exp 04/28"),
      "my card is [card number removed] exp 04/28")
check("dashed, and an Amex", redact("5555-5555-5555-4444 and 378282246310005"),
      "[card number removed] and [card number removed]")
check("a password he says", redact("the wifi password is hunter2!"), "the wifi password is [password removed]")
check("password: value", redact("Password: Sup3r$ecret"), "Password: [password removed]")
check("a PIN", redact("my pin is 4821"), "my pin is [PIN removed]")
check("an SSN", redact("SSN 123-45-6789 on file"), "SSN [SSN removed] on file")
check("keys providers issue", [redact(k) for k in (
    "sk-ant-api03-" + "a" * 40, "ghp_" + "A" * 36, "AKIAIOSFODNN7EXAMPLE", "gsk_" + "b" * 50,
    "GOCSPX-" + "c" * 28, "123456789:" + "A" * 35, "1234~" + "d" * 64, "1//0" + "e" * 40,
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U")],
      ["[secret removed]"] * 9)
check("a login inside a URL", redact("use https://user1:s3cret@bridge.example/simplefin now"),
      "use https://[login removed]@bridge.example/simplefin now")
check("the calendar's secret address", redact("https://calendar.google.com/calendar/ical/me%40gmail.com/private-0123456789abcdef0123/basic.ics"),
      "https://calendar.google.com/calendar/ical/me%40gmail.com/[secret removed]/basic.ics")
check("a private key block", redact("-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n-----END RSA PRIVATE KEY-----"),
      "[secret removed]")
check("what was taken, in words", removed("pw is abcd and 4111111111111111", redact("pw is abcd and 4111111111111111")),
      ["a card number", "a password"])

# -- what stays ---------------------------------------------------------------------------------
for ordinary in ("call me at 303-555-0142", "order #1234567890123 shipped", "tracking 9400111899223100001234",
                 "the password reset link expired", "I spent $1,240.55", "pin it to the top", "2026-09-30 at 7:30",
                 "the 16 digit number 1234 5678 9012 3456", "sk-8 skates", "passcode please"):
    check(f"left alone: {ordinary}", redact(ordinary), ordinary)

# -- outside text, and logs -----------------------------------------------------------------------
check("outside text goes through it: one line", safe_field("Your card 4111111111111111 was charged"),
      "Your card [card number removed] was charged")
check("and a fenced body", unfence("Hi,\nyour temporary password: Xy7!qq9\nThanks"),
      "Hi,\nyour temporary password: [password removed]\nThanks")
stream = io.StringIO()
handler = logging.StreamHandler(stream)
handler.addFilter(LogScrubber())
logger = logging.getLogger("test_redact")
logger.addHandler(handler)
logger.propagate = False
logger.warning("refresh failed for %s with key %s", "https://u:pw12345@x.example/a", "gsk_" + "z" * 48)
check("a log line never carries one", stream.getvalue().strip(),
      "refresh failed for https://[login removed]@x.example/a with key [secret removed]")


# -- the conversation log -----------------------------------------------------------------------
async def integration() -> None:
    from sloane.memory.store import Store

    config = isolated(database_url=os.environ["DATABASE_URL"])
    async with Store(config) as store:
        await store._exec("delete from messages where chat_id = 424242")
        await store.log_message(chat_id=424242, direction="in", kind="text", update_id=None,
                                body="remember my locker code, the password is 12-34-56")
        await store.log_message(chat_id=424242, direction="in", kind="voice", update_id=987654321, body="(voice)")
        await store.set_message_body(987654321, "my card is 4111 1111 1111 1111")
        rows = await store._fetch("select body from messages where chat_id = 424242 order by id")
        check("the conversation log never holds one, typed or spoken",
              sorted(r["body"] for r in rows),
              ["my card is [card number removed]", "remember my locker code, the password is [password removed]"])
        await store._exec("delete from messages where chat_id = 424242")


if os.environ.get("DATABASE_URL"):
    asyncio.run(integration())

if FAILURES:
    print(f"FAIL ({len(FAILURES)})")
    for f in FAILURES:
        print("  -", f)
    raise SystemExit(1)
print("redact: cards, keys, passwords, PINs, SSNs and logins out; ordinary numbers in; outside text, logs, the conversation")
