"""What changed in Canvas since the last sync, said once, in one message.

Four kinds, and only these, because they are the ones he would act on:

    new        a new assignment that isn't already past due
    graded     it just got a grade (with the score when Canvas gives one)
    missing    Canvas just flagged it missing
    due_moved  the due date moved, and it's still ahead

The first sync ever is a baseline and announces nothing; otherwise the first
message would be every assignment of the semester. Rendering is rules, not a
model, so the alert still arrives when every provider is down.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sloane.memory.store import Store

Row = dict[str, Any]

LABELS = {"new": "NEW", "graded": "GRADED", "missing": "MISSING", "due_moved": "MOVED"}
MAX_LINES = 12


def _due(moment: datetime, tz: str) -> str:
    local = moment.astimezone(ZoneInfo(tz))
    hour = local.hour % 12 or 12
    return f"{local:%a %b} {local.day} {hour}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"


def _score(value: Any) -> str:
    return f"{float(value):g}"


def classify(before: Row | None, after: Row | None, *, now: datetime, tz: str) -> list[tuple[str, str]]:
    """(kind, detail) for each change worth telling him about."""
    if after is None:
        return []
    due = after.get("due_at")
    status = after.get("status")
    if before is None:
        if status == "open" and (due is None or due > now):
            return [("new", f"due {_due(due, tz)}" if due else "no due date")]
        return []

    found: list[tuple[str, str]] = []
    if status == "graded" and before.get("status") != "graded":
        earned, possible = after.get("points_earned"), after.get("points_possible")
        found.append(("graded", f"{_score(earned)}/{_score(possible)}"
                      if earned is not None and possible else ""))
    if status == "missing" and before.get("status") != "missing":
        found.append(("missing", ""))
    old_due = before.get("due_at")
    if (
        status == "open" and due is not None and old_due is not None
        and abs(due - old_due) > timedelta(minutes=1) and due > now
    ):
        found.append(("due_moved", f"now due {_due(due, tz)}"))
    return found


def render(rows: Sequence[Row]) -> str:
    """One message: a count line, then one line per change, capped."""
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["kind"]] = counts.get(r["kind"], 0) + 1
    order = ["new", "graded", "missing", "due_moved"]
    head = ", ".join(f"{counts[k]} {LABELS[k].lower()}" for k in order if counts.get(k))
    lines = [f"📚 Canvas: {head}"]
    ranked = sorted(rows, key=lambda r: (order.index(r["kind"]), r.get("detected_at") or 0))
    for r in ranked[:MAX_LINES]:
        course = f" [{r['course']}]" if r.get("course") else ""
        detail = r.get("detail") or ""
        sep = ": " if r["kind"] == "graded" and detail else (" — " if detail else "")
        lines.append(f"• {LABELS[r['kind']]} {r['title']}{course}{sep}{detail}")
    if len(rows) > MAX_LINES:
        lines.append(f"…and {len(rows) - MAX_LINES} more")
    return "\n".join(lines)


async def announce(store: Store, say: Callable[[str], Awaitable[None]]) -> int:
    """Send pending changes as one message. Returns how many were announced.

    Claimed before sending and released if the send fails, so a Telegram
    outage delays the alert rather than losing it or doubling it.
    """
    rows = await store.claim_school_changes()
    if not rows:
        return 0
    try:
        await say(render(rows))
    except Exception:
        await store.unclaim_school_changes([str(r["id"]) for r in rows])
        raise
    return len(rows)
