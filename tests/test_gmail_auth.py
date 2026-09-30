"""scripts/gmail_auth.py against a stub Google: it asks for the OAuth client when .env hasn't got it
(the secret hidden), saves it, and trades the pasted address's code for a refresh token, printing
none of the secrets. No network, no database."""

from __future__ import annotations

import builtins
import contextlib
import io
import json
import sys
import tempfile
import threading
import types
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import gmail_auth  # noqa: E402

FAILURES: list[str] = []
CLIENT_ID = "1234-abc.apps.googleusercontent.com"
SECRET = "GOCSPX-client-secret-value"


def check(label: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{label}\n     got: {got!r}\n    want: {want!r}")


class Google(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        form = {k: v[0] for k, v in parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode()).items()}
        ok = form.get("code") == "the-code" and form.get("client_secret") == SECRET and form.get("client_id") == CLIENT_ID
        body = {"access_token": "a", "refresh_token": "1//refresh-token-value",
                "scope": " ".join(gmail_auth.SCOPES)} if ok else {"error": "invalid_grant"}
        raw = json.dumps(body).encode()
        self.send_response(200 if ok else 400)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


server = HTTPServer(("127.0.0.1", 0), Google)
threading.Thread(target=server.serve_forever, daemon=True).start()
gmail_auth.TOKEN_URL = f"http://127.0.0.1:{server.server_port}/token"
gmail_auth.secrets = types.SimpleNamespace(token_urlsafe=lambda n: "state-from-this-run")


def run(env: Path, typed: list[str], hidden: list[str]) -> tuple[int, str]:
    printed = io.StringIO()
    answers, secret_answers = iter(typed), iter(hidden)
    was_input, was_getpass = builtins.input, gmail_auth.getpass
    builtins.input = lambda prompt="": next(answers)
    gmail_auth.getpass = types.SimpleNamespace(getpass=lambda prompt="": next(secret_answers))
    try:
        with contextlib.redirect_stdout(printed):
            status = gmail_auth.main([str(env)])
    finally:
        builtins.input, gmail_auth.getpass = was_input, was_getpass
    return status, printed.getvalue()


folder = Path(tempfile.mkdtemp())
env = folder / ".env"
env.write_text("TELEGRAM_CHAT_ID=1\n")
address = "http://localhost:8765/?state=state-from-this-run&code=the-code&scope=x"
status, said = run(env, [CLIENT_ID, address], [SECRET])
saved = env.read_text()
check("asked for the client, saved it with the token; the secret and token never printed",
      (status, f"GMAIL_CLIENT_ID={CLIENT_ID}" in saved, f"GMAIL_CLIENT_SECRET={SECRET}" in saved,
       "GMAIL_REFRESH_TOKEN=1//refresh-token-value" in saved, SECRET in said, "refresh-token-value" in said,
       "TELEGRAM_CHAT_ID=1" in saved, oct(env.stat().st_mode & 0o777)),
      (0, True, True, True, False, False, True, "0o600"))
check("says to use an incognito window (a pile of cookies broke Whoop's)", "incognito" in said, True)

status, said = run(env, [address], [])
check("with the client in .env already, it doesn't ask again", status, 0)

fresh = folder / ".env2"
fresh.write_text("")
status, said = run(fresh, ["not-a-client-id"], ["x"])
check("something that isn't a client ID: said plainly, no link", (status, "accounts.google.com" in said), (1, False))

status, said = run(env, ["http://localhost:8765/?state=someone-else&code=the-code"], [])
check("an address from another run is refused", (status, "state mismatch" in said), (1, True))
server.shutdown()

if FAILURES:
    print(f"FAIL ({len(FAILURES)})")
    for f in FAILURES:
        print("  -", f)
    raise SystemExit(1)
print("gmail_auth: asks for the client, hides the secret, saves all three, prints none of them")
