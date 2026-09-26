"""The first thing she says after an install or an upgrade: that she's up.

Said once per version of her code (a fingerprint of the package's source), so a
restart doesn't repeat it but an upgrade does -- which is exactly when he is at
the terminal wondering whether it worked. No model is involved, and a send that
fails is tried again on the next start.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

log = logging.getLogger(__name__)

PACKAGE = Path(__file__).resolve().parent
PREFIX = "hello:"

FIRST = ("Sloane here, up and running. Say hi, or try \"what's due tomorrow?\", "
         "/college add CU Boulder EA nov 1, or /roleplay. /help lists everything.")
UPDATED = "Updated and back up. /help lists everything; /status says if anything needs you."


def fingerprint(root: Path = PACKAGE) -> str:
    """A short hash of every source file under `root`: it changes when her code does."""
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


async def announce(store, say: Callable[[str], Awaitable[None]], *, root: Path = PACKAGE) -> str | None:  # noqa: ANN001
    """Tell him she's up, once per version. Returns what was said, or None."""
    key = PREFIX + fingerprint(root)
    seen_before = await store.nudge_prefix_said(PREFIX)
    if not await store.claim_nudges([key]):
        return None  # this version already said hello
    text = UPDATED if seen_before else FIRST
    try:
        await say(text)
    except Exception as exc:  # noqa: BLE001 - try again on the next start
        log.warning("could not say hello, will try on the next start: %s", exc)
        await store.unclaim_nudges([key])
        return None
    return text
