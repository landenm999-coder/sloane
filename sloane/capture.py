"""POST /capture: the connection Capture uses to hand Sloane what Landen said.

Capture (a separate repo, a voice-capture PWA on his phone) records a thought
or a voice note, transcribes it, and posts the text here. It becomes an
episode *in his own voice*, trusted, because he is the one who said it, so it
is recallable later ("what was that idea I had about the bakery site?"). And if
it's a reminder ("remind me tomorrow at 7 to bring the lab"), it is set,
by the same rules the chat uses.

This is the only endpoint meant to be reached from off the box, so it is the
only one that checks a credential:

* It is disabled unless CAPTURE_TOKEN is set, and a token shorter than 32
  characters also counts as unset.
* It needs `Authorization: Bearer <token>`, compared in constant time.
* The body is capped, and the text is never logged.

Reaching it from the phone is done without opening a port (Tailscale serve);
see DEPLOY.md.
"""

from __future__ import annotations

import hmac
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sloane.config import Settings
from sloane.ingest import safe_field
from sloane.memory.embed import EmbedUnavailable
from sloane.memory.store import Store, remember
from sloane.reminders import REMIND_ME

log = logging.getLogger(__name__)

MIN_TOKEN = 32
MAX_TEXT = 20_000
KINDS = ("note", "transcript")


@dataclass
class Result:
    status: int
    body: dict


def enabled(config: Settings) -> bool:
    return len(config.capture_token or "") >= MIN_TOKEN


def authorized(config: Settings, header: str | None) -> bool:
    if not enabled(config):
        return False
    scheme, _, token = (header or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        return False
    return hmac.compare_digest(token.strip().encode(), config.capture_token.encode())


async def ingest(
    store: Store,
    config: Settings,
    payload: Any,
    authorization: str | None,
    *,
    embedder=None,  # noqa: ANN001 - optional: stored unembedded without it
    now: datetime | None = None,
) -> Result:
    if not enabled(config):
        return Result(503, {"error": "capture is not enabled on this box"})
    if not authorized(config, authorization):
        return Result(401, {"error": "unauthorized"})
    if not isinstance(payload, dict):
        return Result(400, {"error": "expected a JSON object"})

    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        return Result(400, {"error": "text is required"})
    if len(text) > MAX_TEXT:
        return Result(413, {"error": f"text is over {MAX_TEXT} characters"})
    kind = payload.get("kind", "note")
    if kind not in KINDS:
        return Result(400, {"error": f"kind must be one of {', '.join(KINDS)}"})
    zone = ZoneInfo(config.timezone)
    when = now or datetime.now(zone)
    captured_at = None
    if payload.get("captured_at") is not None:
        try:
            captured_at = datetime.fromisoformat(str(payload["captured_at"]).replace("Z", "+00:00"))
        except ValueError:
            return Result(400, {"error": "captured_at must be ISO 8601"})
        if captured_at.tzinfo is None:
            captured_at = captured_at.replace(tzinfo=zone)
        if captured_at > when:
            captured_at = when  # a phone clock ahead of ours is not the future

    text = text.strip()
    vector = None
    if embedder is not None:
        try:
            vector = await embedder.embed_one(text[:2000])
        except EmbedUnavailable:
            vector = None
    episode_id = await store.add_episode(
        text, role="user", channel="capture", summary=safe_field(text, limit=160),
        embedding=vector, occurred_at=captured_at, trusted=True, source="capture",
    )
    body: dict[str, Any] = {"stored": True, "id": episode_id, "kind": kind}

    # A captured "remind me ..." is a reminder, read by the chat's own rules.
    asked = REMIND_ME.match(text)
    if asked:
        from sloane.reminders import parse, spoken

        parsed = parse(asked.group(1), when.astimezone(zone))
        if parsed and parsed.text:
            await remember("capture reminder", store.add_reminder(
                text=safe_field(parsed.text, limit=300), due_at=parsed.due, source="capture",
            ))
            body["reminder"] = f"{spoken(parsed.due, when.astimezone(zone))}: {safe_field(parsed.text, limit=300)}"
        else:
            body["reminder"] = None
            body["note"] = "sounded like a reminder, but no time could be read; stored as a note"
    log.info("capture stored: %s, %s chars", kind, len(text))
    return Result(201, body)
