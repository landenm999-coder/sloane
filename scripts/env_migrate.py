#!/usr/bin/env python3
"""Bring an existing .env's old defaults up to date. Run by install.sh on every upgrade.

The installer writes .env once, from .env.example, and never overwrites it. So
when a default changes in the code -- a provider retires a model, a memory
window grows -- the box keeps the old value forever, because .env pins it.
This moves a setting only when it still holds exactly the old default: a value
he chose himself is his, and is left alone. It prints the names it changed,
never values (none of these are secrets, but the habit is the point).

    python3 scripts/env_migrate.py /opt/sloane/.env
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# (key, the old default, the new one). Append; never rewrite history here.
MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    # Groq retired it (404 model_not_found); the bulk lane was falling back to the CLI.
    ("GROQ_MODEL", "llama-3.3-70b-versatile", "openai/gpt-oss-120b"),
    # A day of conversation, not half of one (the memory upgrade, 2026-09-28).
    ("BUDGET_CONVERSATION", "1500", "2500"),
    ("CONVERSATION_HOURS", "12", "24"),
    ("CONVERSATION_MESSAGES", "24", "40"),
)


def migrate(text: str) -> tuple[str, list[str]]:
    """The .env text with old defaults moved on, and the keys that moved."""
    lines = text.splitlines()
    changed: list[str] = []
    for i, line in enumerate(lines):
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, _, value = line.partition("=")
        key, bare = key.strip(), value.strip().strip("'\"")
        for name, old, new in MIGRATIONS:
            if key == name and bare == old:
                lines[i] = f"{key}={new}"
                changed.append(key)
    out = "\n".join(lines) + ("\n" if text.endswith("\n") or not text else "")
    return out, changed


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: env_migrate.py PATH_TO_.env", file=sys.stderr)
        return 2
    path = Path(argv[1])
    if not path.exists():
        return 0
    text = path.read_text()
    out, changed = migrate(text)
    if not changed:
        return 0
    # Same mode as before (600), written whole then moved into place.
    mode = path.stat().st_mode & 0o777
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".env.migrate.")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(out)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    print("Updated old defaults in .env: " + ", ".join(changed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
