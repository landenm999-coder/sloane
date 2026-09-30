"""Secrets out of text: before it reaches a model, the conversation log, or a log line.

Landen's rule is that credentials never go in chat, logs or replies. This is the
code that holds it when something slips anyway: a password he types to her, a card
number in an email, a token in a pasted error. Each is replaced with a plain mark
("[card number removed]") so the sentence still reads.

Narrow on purpose. A card number is 13-19 digits that start the way cards start
and pass the Luhn check (not a phone number or an order number); a key is one of
the shapes providers actually issue; a password is a value said to be one. What
isn't clearly a secret is left alone: a scrubber that eats ordinary numbers is
worse than none.

Used by `ingest.safe_field`/`ingest.unfence` (all outside text), `Store.log_message`
(everything in the conversation), `Bot.respond` (what the model sees of his words)
and `LogScrubber` (every log line).
"""

from __future__ import annotations

import logging
import re

CARD = "[card number removed]"
SECRET = "[secret removed]"
PASSWORD = "[password removed]"

_CARD = re.compile(r"(?<![\d-])(?:\d[ -]?){12,18}\d(?![\d-])")
_CARD_START = re.compile(r"^(?:4|5[1-5]|2[2-7]|3[47]|6(?:011|5))")
_SSN = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")
_URL_LOGIN = re.compile(r"\b(https?://)[^\s/:@]+:[^\s/@]+@", re.I)
_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)
_KEYS = re.compile(
    r"\b(?:sk-ant-[A-Za-z0-9_-]{20,}|sk-(?:proj-)?[A-Za-z0-9_-]{24,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}"
    r"|AKIA[0-9A-Z]{16}|xox[abposr]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{35}|gsk_[A-Za-z0-9]{40,}|GOCSPX-[A-Za-z0-9_-]{20,}"
    r"|1//0[A-Za-z0-9_-]{30,}|\d{8,10}:[A-Za-z0-9_-]{35}|\d{3,6}~[A-Za-z0-9]{40,}"
    r"|eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})"
)
_ICS = re.compile(r"(/calendar/ical/[^/\s]+/)private-[0-9a-f]{16,}", re.I)
_SAID_PASSWORD = re.compile(
    r"\b((?:pass(?:word|code|phrase)?|passwd|pwd|pw)(?:\s+(?:is|was|=|:)|\s*[:=])\s*)(?!\[)[\"']?[^\s\"']{3,}[\"']?", re.I)
_SAID_PIN = re.compile(r"\b((?:pin|pin\s+number|pin\s+code)(?:\s+(?:is|was)|\s*[:=])\s*)\d{4,8}\b", re.I)


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return total % 10 == 0


def _card(match: re.Match[str]) -> str:
    digits = re.sub(r"\D", "", match.group(0))
    if 13 <= len(digits) <= 19 and _CARD_START.match(digits) and _luhn(digits):
        return CARD
    return match.group(0)


def redact(text: str) -> str:
    """`text` with anything that is clearly a secret replaced by a mark."""
    if not text or not isinstance(text, str):
        return text
    out = _PRIVATE_KEY.sub(SECRET, text)
    out = _URL_LOGIN.sub(lambda m: m.group(1) + "[login removed]@", out)
    out = _ICS.sub(lambda m: m.group(1) + "[secret removed]", out)
    out = _KEYS.sub(SECRET, out)
    out = _CARD.sub(_card, out)
    out = _SSN.sub("[SSN removed]", out)
    out = _SAID_PASSWORD.sub(lambda m: m.group(1) + PASSWORD, out)
    out = _SAID_PIN.sub(lambda m: m.group(1) + "[PIN removed]", out)
    return out


def removed(before: str, after: str) -> list[str]:
    """What kinds of thing redact() took out, for telling him: ["a card number", "a password"]."""
    said = []
    for mark, words in ((CARD, "a card number"), (PASSWORD, "a password"), ("[PIN removed]", "a PIN"),
                        ("[SSN removed]", "a Social Security number"), (SECRET, "a key or token"),
                        ("[login removed]", "a login")):
        if after.count(mark) > before.count(mark):
            said.append(words)
    return said


class LogScrubber(logging.Filter):
    """Every log record, secrets out, before any handler writes it."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a broken format string is logging's problem, not ours
            return True
        clean = redact(message)
        if clean != message:
            record.msg, record.args = clean, ()
        return True


def install() -> None:
    """The scrubber on every handler the root logger has (call after logging is configured)."""
    root = logging.getLogger()
    for handler in root.handlers:
        if not any(isinstance(f, LogScrubber) for f in handler.filters):
            handler.addFilter(LogScrubber())
