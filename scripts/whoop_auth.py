"""One-time Whoop consent. Mints WHOOP_REFRESH_TOKEN and writes it into .env.

Standard library only, like gmail_auth.py: run it with the box's own python3,
outside Docker, so it can write .env as the user who owns it.

    cd /opt/sloane
    python3 scripts/whoop_auth.py
    sudo systemctl restart sloane

First make an app at developer.whoop.com (DEPLOY 7j) with the redirect URL
http://localhost:8765 and put its WHOOP_CLIENT_ID and WHOOP_CLIENT_SECRET in
.env. This prints a Whoop link; sign in, allow it, and paste back the address
your browser lands on (the page itself won't load, and doesn't need to).

Whoop hands out a new refresh token every time one is used, so this one works
once: Sloane swaps it on her first call and keeps the current one on her volume
(WHOOP_TOKEN_FILE). Running this again starts her over with a fresh one.

Read-only scopes: recovery, cycles (strain), sleep, workouts, profile, and
offline (the refresh token). The token is written to .env and never printed.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gmail_auth import read_env, write_env  # noqa: E402

BASE = os.environ.get("WHOOP_API_BASE", "https://api.prod.whoop.com").rstrip("/")
AUTH_URL = f"{BASE}/oauth/oauth2/auth"
TOKEN_URL = f"{BASE}/oauth/oauth2/token"
REDIRECT = "http://localhost:8765"
# Whoop sits behind Cloudflare, which turns urllib's own user-agent away (403, "error code: 1010").
AGENT = "Mozilla/5.0 (compatible; Sloane/1.0)"
SCOPES = "offline read:recovery read:cycles read:sleep read:workout read:profile"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    env_path = Path(args[0]) if args else Path(".env")
    env = read_env(env_path)
    client_id = env.get("WHOOP_CLIENT_ID") or os.environ.get("WHOOP_CLIENT_ID", "")
    client_secret = env.get("WHOOP_CLIENT_SECRET") or os.environ.get("WHOOP_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        print(f"Put WHOOP_CLIENT_ID and WHOOP_CLIENT_SECRET in {env_path} first (DEPLOY.md 7j).")
        return 1

    state = secrets.token_urlsafe(16)  # Whoop wants at least 8 characters
    link = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": REDIRECT, "response_type": "code", "scope": SCOPES, "state": state,
    })
    print("\n1. Open this link in a browser and sign in to Whoop:\n")
    print(link)
    print(
        "\n2. Allow it. The browser then lands on a localhost page that won't load. That's expected.\n"
        "3. Copy the whole address from the address bar and paste it here.\n"
    )
    pasted = input("Address: ").strip()
    query = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query)
    if query.get("error"):
        print(f"Whoop said: {query['error'][0]}. Nothing was saved.")
        return 1
    if query.get("state", [""])[0] != state:
        print("That address is not from this run (state mismatch). Run the script again.")
        return 1
    code = query.get("code", [""])[0]
    if not code:
        print("No code in that address. Copy the full address, starting with http://localhost.")
        return 1

    body = urllib.parse.urlencode({
        "grant_type": "authorization_code", "code": code, "client_id": client_id,
        "client_secret": client_secret, "redirect_uri": REDIRECT,
    }).encode()
    try:
        request = urllib.request.Request(TOKEN_URL, data=body, headers={"User-Agent": AGENT, "Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=30) as resp:
            token = json.load(resp)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        print(f"Whoop refused the exchange (HTTP {exc.code}): {detail}")
        return 1

    refresh = token.get("refresh_token")
    if not refresh:
        print("Whoop returned no refresh token. Check the app has the offline scope, then run this again.")
        return 1
    write_env(env_path, "WHOOP_REFRESH_TOKEN", refresh)
    print(f"\nSaved WHOOP_REFRESH_TOKEN to {env_path} (chmod 600). Now: sudo systemctl restart sloane")
    return 0


if __name__ == "__main__":
    sys.exit(main())
