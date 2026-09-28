"""The first thing she says after an install or an upgrade: that she's up.

Said once per version of her code (a fingerprint of the package's source), so a
restart doesn't repeat it but an upgrade does -- which is exactly when he is at
the terminal wondering whether it worked. No model is involved. A send that
fails is tried again for a while (a bot can't message him until he has pressed
Start in its chat, which a new install often hasn't), then on the next start.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

log = logging.getLogger(__name__)

PACKAGE = Path(__file__).resolve().parent
PREFIX = "hello:"

FIRST = ("Sloane here, up and running. I'll brief you at 6:35 each morning, check in before and after your "
         "shifts, and nudge you ahead of deadlines. Say hi, or try \"what's due tomorrow?\", "
         "/college add CU Boulder EA nov 1, or /roleplay. /help lists everything.")
UPDATED = "Updated and back up. /help lists everything; /status says if anything needs you."
# Seconds between tries after a failed send: about half an hour in all.
RETRIES = (30, 60, 120, 300, 600, 900)


def fingerprint(root: Path = PACKAGE) -> str:
    """A short hash of every source file under `root`: it changes when her code does."""
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


async def announce(store, say: Callable[[str], Awaitable[None]], *, root: Path = PACKAGE,  # noqa: ANN001
                   retries: tuple[float, ...] = RETRIES, silent: bool = False) -> str | None:
    """Tell him she's up, once per version. Returns what was said, or None.

    `silent`: a workshop deploy is about to say what went live, so this
    version is recorded as greeted without a second message.
    """
    key = PREFIX + fingerprint(root)
    if await store.nudge_prefix_said(key):
        return None  # this version already said hello
    if silent and await store.nudge_prefix_said(PREFIX):
        await store.claim_nudges([key])
        return None
    text = UPDATED if await store.nudge_prefix_said(PREFIX) else FIRST
    for wait in (*retries, None):
        try:
            await say(text)
        except Exception as exc:  # noqa: BLE001 - tried again below, then next start
            if wait is None:
                log.warning("could not say hello, will try on the next start: %s", exc)
                return None
            log.warning("could not say hello (has he pressed Start in the bot's chat?), "
                        "trying again in %.0fs: %s", wait, exc)
            await asyncio.sleep(wait)
            continue
        # Recorded only once it's said: a restart mid-retry just tries again.
        await store.claim_nudges([key])
        return text
    return None
