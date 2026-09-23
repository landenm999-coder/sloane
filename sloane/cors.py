"""Cross-origin access for the few endpoints a browser app calls, and no others.

Capture is a web app. On Landen's phone it POSTs straight from the browser to
Sloane over Tailscale, which makes the request cross-origin: its page is on
Vercel, Sloane is on the tailnet. Browsers preflight that request (it carries
an Authorization header) and drop it unless Sloane answers the preflight.

Starlette's CORSMiddleware would answer for every path. That is the wrong
shape here: /facts and /state have no token of their own, so an origin allowed
everywhere is an origin that can read his whole schedule if it is ever
compromised. So this middleware is scoped to named paths, and only for origins
listed in config. Everything else gets no CORS headers at all, which is what it
had before.

It also answers Chrome's Private Network Access preflight
(`Access-Control-Request-Private-Network`), because a tailnet address is a
private one and a public page reaching it is exactly what that check gates.
"""

from __future__ import annotations

from collections.abc import Iterable

from starlette.types import ASGIApp, Message, Receive, Scope, Send

ALLOW_METHODS = "POST, OPTIONS"
ALLOW_HEADERS = "authorization, content-type"
MAX_AGE = "600"


class ScopedCORS:
    """Pure ASGI middleware: CORS for `paths`, from `origins`, and nowhere else."""

    def __init__(self, app: ASGIApp, *, origins: Iterable[str], paths: Iterable[str]) -> None:
        self.app = app
        self.origins = frozenset(o.strip().rstrip("/") for o in origins if o.strip())
        self.paths = frozenset(paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") not in self.paths or not self.origins:
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        origin = headers.get("origin", "")
        if origin not in self.origins:
            await self.app(scope, receive, send)
            return

        allow = [
            (b"access-control-allow-origin", origin.encode("latin-1")),
            (b"vary", b"Origin"),
        ]
        if scope["method"] == "OPTIONS" and "access-control-request-method" in headers:
            preflight = allow + [
                (b"access-control-allow-methods", ALLOW_METHODS.encode()),
                (b"access-control-allow-headers", ALLOW_HEADERS.encode()),
                (b"access-control-max-age", MAX_AGE.encode()),
            ]
            if headers.get("access-control-request-private-network") == "true":
                preflight.append((b"access-control-allow-private-network", b"true"))
            await send({"type": "http.response.start", "status": 204, "headers": preflight})
            await send({"type": "http.response.body", "body": b""})
            return

        async def with_cors(message: Message) -> None:
            if message["type"] == "http.response.start":
                message = {**message, "headers": list(message.get("headers", [])) + allow}
            await send(message)

        await self.app(scope, receive, with_cors)


def origins(value: str) -> list[str]:
    """The comma-separated setting as a list."""
    return [o.strip() for o in value.split(",") if o.strip()]
