"""One-time Gmail consent. Mints GMAIL_REFRESH_TOKEN and writes it into .env.

Standard library only, on purpose: run it with the box's own python3, outside
Docker, so it can write .env as the user who owns it.

    cd /opt/sloane
    python3 scripts/gmail_auth.py

It reads GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET from .env (and asks for them,
the secret hidden as you paste, if they aren't there yet, then saves them), prints
a Google link, and asks you to paste back the address your browser lands on afterwards.
That page will fail to load ("localhost refused to connect") -- that is
expected: the code Google hands back is in the address itself, and nothing
needs to be listening for it.

The token is written to .env and never printed.

Scopes requested: gmail.readonly and gmail.compose. Read, draft and send. No
delete -- there is no scope here that could.
"""

from __future__ import annotations

import base64
import getpass
import hashlib
import json
import os
import secrets
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REDIRECT = "http://localhost:8765"
SCOPES = (
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
)


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def write_env(path: Path, key: str, value: str) -> None:
    """Replace `key=` if present, else append. Keeps every other line as is."""
    lines = path.read_text().splitlines() if path.exists() else []
    out, done = [], False
    for line in lines:
        if line.split("=", 1)[0].strip() == key:
            out.append(f"{key}={value}")
            done = True
        else:
            out.append(line)
    if not done:
        out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n")
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 600: it holds credentials


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge.decode().rstrip("=")


def ask_client(env_path: Path, client_id: str, client_secret: str) -> tuple[str, str]:
    """The OAuth client from Google Cloud (Credentials → your Desktop client), asked for here when
    .env doesn't have it yet, and saved there. The secret is read hidden: never on the screen."""
    if not client_id:
        print("\nFrom console.cloud.google.com → APIs & Services → Credentials → your Desktop OAuth client:")
        client_id = input("Client ID (ends in .apps.googleusercontent.com): ").strip()
        if client_id:
            write_env(env_path, "GMAIL_CLIENT_ID", client_id)
    if not client_secret:
        client_secret = getpass.getpass("Client secret (hidden as you paste): ").strip()
        if client_secret:
            write_env(env_path, "GMAIL_CLIENT_SECRET", client_secret)
    return client_id, client_secret


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    env_path = Path(args[0]) if args else Path(".env")
    env = read_env(env_path)
    client_id = env.get("GMAIL_CLIENT_ID") or os.environ.get("GMAIL_CLIENT_ID", "")
    client_secret = env.get("GMAIL_CLIENT_SECRET") or os.environ.get("GMAIL_CLIENT_SECRET", "")
    client_id, client_secret = ask_client(env_path, client_id, client_secret)
    if not client_id.endswith(".apps.googleusercontent.com") or not client_secret:
        print("That isn't a Google OAuth client (the ID ends in .apps.googleusercontent.com). "
              "Check DEPLOY.md section 7c, steps 1-5.")
        return 1

    verifier, challenge = pkce()
    state = secrets.token_urlsafe(16)
    link = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        # offline + consent is what makes Google issue a refresh token at all.
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })

    print("\n1. Open this link (on your PC is fine), in an incognito window, and sign in to the Gmail\n"
          "   account Sloane should read:\n")
    print(link)
    print(
        "\n2. Choose the account. If Google says the app isn't verified, click "
        "Advanced → Go to (unsafe) -- it's your own app.\n"
        "3. Allow both permissions. The browser then lands on a localhost page "
        "that won't load. That's expected.\n"
        "4. Copy the whole address from the address bar and paste it here.\n"
    )
    pasted = input("Address: ").strip()
    query = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query)
    if query.get("error"):
        print(f"Google said: {query['error'][0]}. Nothing was saved.")
        return 1
    if query.get("state", [""])[0] != state:
        print("That address is not from this run (state mismatch). Run the script again.")
        return 1
    code = query.get("code", [""])[0]
    if not code:
        print("No code in that address. Copy the full address, starting with http://localhost.")
        return 1

    body = urllib.parse.urlencode({
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": REDIRECT,
        "grant_type": "authorization_code",
        "code_verifier": verifier,
    }).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(TOKEN_URL, data=body), timeout=30) as resp:
            token = json.load(resp)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        print(f"Google refused the exchange (HTTP {exc.code}): {detail}")
        return 1

    refresh = token.get("refresh_token")
    if not refresh:
        print(
            "Google returned no refresh token. Remove the app's access at "
            "myaccount.google.com/permissions and run this again."
        )
        return 1
    granted = set(token.get("scope", "").split())
    missing = [s.rsplit("/", 1)[-1] for s in SCOPES if s not in granted]
    if missing:
        print(f"Warning: you did not grant {', '.join(missing)}; she won't be able to do everything.")

    write_env(env_path, "GMAIL_REFRESH_TOKEN", refresh)
    print(
        f"\nSaved GMAIL_REFRESH_TOKEN to {env_path} (chmod 600). "
        "Restart Sloane, then run doctor.py -- the gmail line should PASS.\n"
        "Reminder: the OAuth app must be In production, not Testing, or this "
        "token stops working after 7 days."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
