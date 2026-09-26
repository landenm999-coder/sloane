"""She says she's up once per version: after an install, after an upgrade, not
after a plain restart, and again next start if the send failed.

DESTRUCTIVE: deletes hello:* keys from nudges_said.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _settings import isolated

from sloane.hello import FIRST, UPDATED, announce, fingerprint

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{label}\n     got: {got!r}\n    want: {want!r}")


with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    (root / "a.py").write_text("x = 1\n")
    before = fingerprint(root)
    check("the same code, the same fingerprint", fingerprint(root), before)
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "a.cpython-312.pyc").write_bytes(b"junk")
    check("compiled files don't count", fingerprint(root), before)
    (root / "a.py").write_text("x = 2\n")
    check("changed code, a new fingerprint", fingerprint(root) != before, True)
check("the real package has one", len(fingerprint()), 12)


async def integration() -> None:
    from sloane.memory.store import Store

    config = isolated(database_url=os.environ["DATABASE_URL"])
    async with Store(config) as store:
        await store._exec("delete from nudges_said where key like 'hello:%%'")
        said: list[str] = []

        async def say(text):
            said.append(text)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.py").write_text("version = 1\n")
            check("first install: a proper hello", await announce(store, say, root=root), FIRST)
            check("a restart: nothing", await announce(store, say, root=root), None)
            (root / "a.py").write_text("version = 2\n")

            async def down(text):
                raise RuntimeError("telegram is down")

            check("an upgrade whose hello can't be sent", await announce(store, down, root=root, retries=(0, 0)),
                  None)
            check("is said on the next start", await announce(store, say, root=root), UPDATED)
            check("and only once", (await announce(store, say, root=root), said), (None, [FIRST, UPDATED]))

            # Before he presses Start, Telegram refuses; once he does, the retry lands.
            (root / "a.py").write_text("version = 3\n")
            tries: list[str] = []

            async def not_started_yet(text):
                tries.append(text)
                if len(tries) < 3:
                    raise RuntimeError("telegram sendMessage -> 403: bot can't initiate conversation with a user")
                said.append(text)

            check("a refused hello is tried again until it lands",
                  (await announce(store, not_started_yet, root=root, retries=(0, 0, 0)), len(tries)), (UPDATED, 3))
            check("and not again after", await announce(store, say, root=root), None)

            # A month on, the nightly prune must not make a reboot say its first hello again.
            await store._exec("update nudges_said set offered_at = now() - interval '90 days' "
                              "where key like 'hello:%%'")
            await store.prune_nudges(30)
            check("the prune keeps which versions said hello", await announce(store, say, root=root), None)
        await store._exec("delete from nudges_said where key like 'hello:%%'")


if os.environ.get("DATABASE_URL"):
    asyncio.run(integration())

if FAILURES:
    print(f"FAIL ({len(FAILURES)})")
    for f in FAILURES:
        print("  -", f)
    raise SystemExit(1)
print("hello: once per version, after an install or an upgrade, retried if unsent")
