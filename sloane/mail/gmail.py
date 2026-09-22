"""Gmail over plain REST. httpx only -- no Google client library.

The client library is ~40 MB of transitive dependencies to wrap five HTTP
calls, on a box with 1 GB of RAM per core. These are the five:

    POST oauth2.googleapis.com/token             refresh the access token
    GET  users/me/messages?q=...                 list ids
    GET  users/me/messages/{id}?format=full      one message
    POST users/me/messages/send                  send (only after approval)
    POST users/me/drafts                         save a draft (never sends)

Scopes are `gmail.readonly` and `gmail.compose`. Neither can delete mail --
`delete anything` is a hard line, and the token is not even able to cross it.

Everything that comes back from a message is written by whoever sent it. The
parsed fields are flattened with `safe_field`, and the body is kept as text to
be fenced, never interpreted.
"""

from __future__ import annotations

import base64
import binascii
import html
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from typing import Any

import httpx

from sloane.config import Settings
from sloane.ingest import safe_field
from sloane.mail import MailError

log = logging.getLogger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"
API_BASE = "https://gmail.googleapis.com/gmail/v1"
SCOPES = (
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
)

# What a triage or a draft ever needs of a body. Newsletters run to 100 KB of
# HTML; none of it changes whether the email needs a reply.
BODY_LIMIT = 4000

_ADDRESS = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
_TAGS = re.compile(r"<(script|style)\b.*?</\1>|<[^>]+>", re.I | re.S)
_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_BLANKS = re.compile(r"\n\s*\n\s*\n+")


def valid_address(address: str) -> bool:
    return bool(_ADDRESS.match(address or ""))


@dataclass(frozen=True)
class Message:
    gmail_id: str
    thread_id: str
    sender: str  # the From address, bare and lowercased
    sender_name: str
    reply_to: str  # where a reply goes: Reply-To if set, else From
    subject: str
    snippet: str
    body: str
    received_at: datetime | None
    message_id: str  # RFC 822 Message-ID, for threading a reply
    references: str
    to: str = ""


def _b64(data: str) -> str:
    """Gmail's body data is base64url, with padding sometimes stripped."""
    try:
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except (binascii.Error, ValueError):
        return ""
    return raw.decode("utf-8", errors="replace")


def _html_to_text(text: str) -> str:
    text = re.sub(r"<br\s*/?>|</p>|</div>|</tr>", "\n", text, flags=re.I)
    return html.unescape(_TAGS.sub(" ", text))


def _body(payload: dict) -> str:
    """Prefer text/plain anywhere in the tree; fall back to stripped HTML."""
    plain: list[str] = []
    rich: list[str] = []

    def walk(part: dict) -> None:
        mime = (part.get("mimeType") or "").lower()
        data = (part.get("body") or {}).get("data")
        # An attachment has a filename; its bytes are not the message.
        if data and not part.get("filename"):
            if mime == "text/plain":
                plain.append(_b64(data))
            elif mime == "text/html":
                rich.append(_html_to_text(_b64(data)))
        for child in part.get("parts") or []:
            walk(child)

    walk(payload)
    text = "\n".join(plain) if plain else "\n".join(rich)
    text = _BLANKS.sub("\n\n", text.replace("\r\n", "\n")).strip()
    return text[:BODY_LIMIT]


def parse_message(raw: dict) -> Message:
    payload = raw.get("payload") or {}
    headers = {
        (h.get("name") or "").lower(): h.get("value") or ""
        for h in payload.get("headers") or []
    }
    name, address = parseaddr(headers.get("from", ""))
    _, reply_to = parseaddr(headers.get("reply-to", ""))
    received: datetime | None = None
    if raw.get("internalDate"):
        try:
            received = datetime.fromtimestamp(int(raw["internalDate"]) / 1000, tz=timezone.utc)
        except (TypeError, ValueError):
            received = None
    if received is None and headers.get("date"):
        try:
            received = parsedate_to_datetime(headers["date"])
        except (TypeError, ValueError):
            received = None
    return Message(
        gmail_id=str(raw.get("id", "")),
        thread_id=str(raw.get("threadId", "")),
        sender=address.strip().lower(),
        sender_name=safe_field(name, limit=120),
        reply_to=(reply_to or address).strip().lower(),
        subject=safe_field(headers.get("subject", ""), limit=250),
        snippet=safe_field(html.unescape(raw.get("snippet", "")), limit=300),
        body=_body(payload),
        received_at=received,
        message_id=safe_field(headers.get("message-id", ""), limit=500),
        references=safe_field(headers.get("references", ""), limit=2000),
        to=", ".join(a for _, a in getaddresses([headers.get("to", "")]) if a),
    )


def reply_subject(subject: str) -> str:
    subject = safe_field(subject, limit=250)
    return subject if subject.lower().startswith("re:") else f"Re: {subject}".strip()


def build_raw(
    *,
    to: str,
    subject: str,
    body: str,
    in_reply_to: str = "",
    references: str = "",
) -> str:
    """An RFC 822 message, base64url-encoded the way Gmail wants it.

    Every header value came, at some point, from someone else's email. They are
    flattened to one line first, and `EmailMessage` refuses anything that still
    carries a line break, so a subject cannot smuggle in a Bcc.
    """
    if not valid_address(to):
        raise MailError(f"not a sendable address: {to!r}")
    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = safe_field(subject, limit=250)
    if in_reply_to:
        msg["In-Reply-To"] = safe_field(in_reply_to, limit=500)
        msg["References"] = safe_field(f"{references} {in_reply_to}".strip(), limit=2000)
    msg.set_content(body or "")
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")


class GmailClient:
    def __init__(
        self,
        config: Settings,
        *,
        token_url: str = TOKEN_URL,
        api_base: str = API_BASE,
        timeout: float = 30.0,
    ) -> None:
        self._config = config
        self._token_url = config.gmail_token_url or token_url
        self._base = (config.gmail_api_base or api_base).rstrip("/")
        self._timeout = timeout
        self._access: str | None = None
        self._expires = 0.0

    @property
    def configured(self) -> bool:
        c = self._config
        return bool(c.gmail_client_id and c.gmail_client_secret and c.gmail_refresh_token)

    async def _token(self, client: httpx.AsyncClient) -> str:
        if self._access and time.monotonic() < self._expires:
            return self._access
        try:
            resp = await client.post(
                self._token_url,
                data={
                    "client_id": self._config.gmail_client_id,
                    "client_secret": self._config.gmail_client_secret,
                    "refresh_token": self._config.gmail_refresh_token,
                    "grant_type": "refresh_token",
                },
            )
        except httpx.HTTPError as exc:
            raise MailError(f"could not reach Google to refresh the token: {exc}") from exc
        if resp.status_code != 200:
            reason = ""
            try:
                reason = resp.json().get("error", "")
            except ValueError:
                pass
            if reason == "invalid_grant":
                raise MailError(
                    "Gmail access was revoked or expired. Run scripts/gmail_auth.py "
                    "again (and check the OAuth app is In production, not Testing, "
                    "or the token dies every 7 days)."
                )
            raise MailError(f"token refresh failed: HTTP {resp.status_code} {reason}".strip())
        body = resp.json()
        self._access = body.get("access_token")
        if not self._access:
            raise MailError("token refresh returned no access token")
        # Refresh a minute early rather than race the expiry mid-request.
        self._expires = time.monotonic() + max(60, int(body.get("expires_in", 3600)) - 60)
        return self._access

    async def _call(self, method: str, path: str, **kwargs: Any) -> dict:
        if not self.configured:
            raise MailError("Gmail is not configured")
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            for attempt in (1, 2):
                token = await self._token(client)
                try:
                    resp = await client.request(
                        method, f"{self._base}/{path}",
                        headers={"Authorization": f"Bearer {token}"}, **kwargs,
                    )
                except httpx.HTTPError as exc:
                    raise MailError(f"Gmail unreachable: {exc}") from exc
                # A 401 on a token we thought was fresh: drop it and retry once.
                if resp.status_code == 401 and attempt == 1:
                    self._access = None
                    continue
                if resp.status_code >= 400:
                    raise MailError(f"Gmail {method} {path.split('?')[0]}: HTTP {resp.status_code}")
                return resp.json() if resp.content else {}
        raise MailError("Gmail rejected the refreshed token")

    # -- reading -----------------------------------------------------------------

    async def profile(self) -> dict:
        return await self._call("GET", "users/me/profile")

    async def list_ids(self, query: str, limit: int) -> list[str]:
        data = await self._call(
            "GET", "users/me/messages", params={"q": query, "maxResults": max(1, limit)}
        )
        return [str(m["id"]) for m in data.get("messages") or [] if m.get("id")][:limit]

    async def get(self, gmail_id: str) -> Message:
        if not _ID.match(gmail_id):
            raise MailError(f"not a Gmail message id: {gmail_id!r}")
        raw = await self._call("GET", f"users/me/messages/{gmail_id}", params={"format": "full"})
        return parse_message(raw)

    # -- writing (only ever reached through an approved proposal) ---------------

    async def send(self, raw: str, *, thread_id: str = "") -> str:
        body: dict[str, Any] = {"raw": raw}
        if thread_id:
            body["threadId"] = thread_id
        data = await self._call("POST", "users/me/messages/send", json=body)
        return str(data.get("id", ""))

    async def create_draft(self, raw: str, *, thread_id: str = "") -> str:
        message: dict[str, Any] = {"raw": raw}
        if thread_id:
            message["threadId"] = thread_id
        data = await self._call("POST", "users/me/drafts", json={"message": message})
        return str(data.get("id", ""))
