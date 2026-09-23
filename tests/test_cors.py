"""Scoped CORS: the Capture origin may call /capture from a browser, and nothing else.

The failure this guards against is the easy one: turning on CORS for Capture
with Starlette's global middleware, which would also let that origin read
/facts and /state -- endpoints with no token of their own.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from sloane.cors import ScopedCORS, origins

FAILURES: list[str] = []
CAPTURE = "https://capture.example.app"


def check(label: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{label}\n     got: {got!r}\n    want: {want!r}")


app = FastAPI()


@app.post("/capture")
async def capture() -> dict:
    return {"stored": True}


@app.get("/facts")
async def facts() -> dict:
    return {"due": ["everything"]}


app.add_middleware(ScopedCORS, origins=origins(f"{CAPTURE}/, https://other.example"), paths={"/capture"})
client = TestClient(app)

preflight = client.options("/capture", headers={
    "Origin": CAPTURE,
    "Access-Control-Request-Method": "POST",
    "Access-Control-Request-Headers": "authorization, content-type",
    "Access-Control-Request-Private-Network": "true",
})
check("preflight from Capture succeeds", preflight.status_code, 204)
check("and names the origin", preflight.headers.get("access-control-allow-origin"), CAPTURE)
check("allows the Authorization header", "authorization" in preflight.headers.get("access-control-allow-headers", ""), True)
check("answers Chrome's private-network check", preflight.headers.get("access-control-allow-private-network"), "true")

posted = client.post("/capture", headers={"Origin": CAPTURE}, json={"text": "x"})
check("the POST carries the origin back", posted.headers.get("access-control-allow-origin"), CAPTURE)
check("and still reaches the endpoint", posted.json(), {"stored": True})

stranger = client.options("/capture", headers={"Origin": "https://evil.example",
                                               "Access-Control-Request-Method": "POST"})
check("an unlisted origin gets no CORS", stranger.headers.get("access-control-allow-origin"), None)
check("its preflight is not answered with 204", stranger.status_code != 204, True)

read = client.get("/facts", headers={"Origin": CAPTURE})
check("/facts never gets CORS, even for Capture", read.headers.get("access-control-allow-origin"), None)
facts_preflight = client.options("/facts", headers={"Origin": CAPTURE, "Access-Control-Request-Method": "GET"})
check("nor a preflight answer", facts_preflight.headers.get("access-control-allow-origin"), None)

check("no origins configured means nothing is allowed", origins(""), [])

# The real app is wired with it, scoped to /capture.
import sloane.main  # noqa: E402

wired = [m for m in sloane.main.app.user_middleware if m.cls is ScopedCORS]
check("main.py installs ScopedCORS", len(wired), 1)
check("scoped to the token-checked endpoints", sorted(sloane.main.CORS_PATHS), ["/capture"])

if FAILURES:
    print(f"FAIL ({len(FAILURES)})")
    for f in FAILURES:
        print("  -", f)
    raise SystemExit(1)
print("cors: Capture may POST /capture cross-origin; nothing else is exposed")
