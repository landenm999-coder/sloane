"""One-time SimpleFIN claim: his setup token becomes SIMPLEFIN_ACCESS_URL in .env.

Standard library only, on purpose: run it with the box's own python3, outside
Docker, so it can write .env as the user who owns it.

    cd /opt/sloane
    python3 scripts/simplefin_auth.py

At bridge.simplefin.org, connect your bank, card and Fidelity, then make a
setup token (My Account → New connection). Paste it here; it doesn't show as
you paste. The token is a one-time claim: this trades it for the access URL,
which is written to .env and never printed. The access URL is read-only (no
way to move money), but it opens every account you connected: a password.
"""

from __future__ import annotations

import base64
import binascii
import getpass
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gmail_auth import write_env  # noqa: E402

# Bot blockers turn Python's own user-agent away (Whoop's did: 403, "error code: 1010").
AGENT = "Mozilla/5.0 (compatible; Sloane/1.0)"


def claim_url(token: str) -> str | None:
    """The address a setup token decodes to, or None if it isn't one."""
    raw = "".join(token.split())
    try:
        url = base64.b64decode(raw + "=" * (-len(raw) % 4), altchars=b"-_" if ("-" in raw or "_" in raw) else None,
                               validate=False).decode()
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    parts = urllib.parse.urlsplit(url.strip())
    local = parts.hostname in ("127.0.0.1", "localhost")
    if parts.scheme != "https" and not (parts.scheme == "http" and local):
        return None
    return url.strip()


def access_ok(url: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    local = parts.hostname in ("127.0.0.1", "localhost")
    return (parts.scheme == "https" or (parts.scheme == "http" and local)) and bool(parts.username and parts.password)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    env_path = Path(args[0]) if args else Path(".env")
    print("\nPaste the setup token from bridge.simplefin.org (My Account → New connection).")
    token = getpass.getpass("Setup token (hidden as you paste): ").strip()
    url = claim_url(token)
    if not url:
        print("That isn't a SimpleFIN setup token. Copy the whole token and run this again. Nothing was saved.")
        return 1
    request = urllib.request.Request(url, data=b"", method="POST",
                                     headers={"User-Agent": AGENT, "Content-Length": "0"})
    try:
        with urllib.request.urlopen(request, timeout=60) as resp:
            access = resp.read().decode(errors="replace").strip()
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            print("SimpleFIN says that token was already used or isn't valid. Make a new setup token and run "
                  "this again. Nothing was saved.")
        else:
            print(f"SimpleFIN refused the claim (HTTP {exc.code}). Nothing was saved.")
        return 1
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"Couldn't reach SimpleFIN ({type(exc).__name__}). Check the box's internet and try again. "
              "The token is still unused.")
        return 1
    if not access_ok(access):
        print("SimpleFIN's answer wasn't an access URL. Nothing was saved; make a new setup token and try again.")
        return 1
    write_env(env_path, "SIMPLEFIN_ACCESS_URL", access)
    print(f"\nSaved SIMPLEFIN_ACCESS_URL to {env_path} (chmod 600). Restart Sloane "
          "(sudo systemctl restart sloane); she syncs within a minute of asking (/bank sync), then every three hours.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
