"""Canvas change classification and the alert text. Pure; no database."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sloane.school.changes import MAX_LINES, classify, render

FAILURES: list[str] = []
NOW = datetime(2026, 9, 22, 16, 0, tzinfo=timezone.utc)
TZ = "America/Denver"
LATER = NOW + timedelta(days=3)


def check(label: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{label}\n     got: {got!r}\n    want: {want!r}")


def row(status="open", due=LATER, earned=None, possible=None):
    return {"id": "x", "status": status, "due_at": due, "points_earned": earned, "points_possible": possible}


check("a new future assignment is news", classify(None, row(), now=NOW, tz=TZ),
      [("new", "due Fri Sep 25 10:00 AM")])
check("a new one with no due date is news", classify(None, row(due=None), now=NOW, tz=TZ),
      [("new", "no due date")])
check("a new one already past due is not", classify(None, row(due=NOW - timedelta(days=1)), now=NOW, tz=TZ), [])
check("a new one already graded is not", classify(None, row(status="graded"), now=NOW, tz=TZ), [])
check("graded, with a score", classify(row(), row(status="graded", earned=18, possible=20), now=NOW, tz=TZ),
      [("graded", "18/20")])
check("graded, fractional score", classify(row(), row(status="graded", earned=18.5, possible=20), now=NOW, tz=TZ),
      [("graded", "18.5/20")])
check("graded without points", classify(row(), row(status="graded"), now=NOW, tz=TZ), [("graded", "")])
check("still graded is not news", classify(row(status="graded"), row(status="graded", earned=1, possible=2),
                                           now=NOW, tz=TZ), [])
check("newly missing", classify(row(), row(status="missing"), now=NOW, tz=TZ), [("missing", "")])
check("still missing is not news", classify(row(status="missing"), row(status="missing"), now=NOW, tz=TZ), [])
check("a moved due date", classify(row(), row(due=LATER + timedelta(days=1)), now=NOW, tz=TZ),
      [("due_moved", "now due Sat Sep 26 10:00 AM")])
check("a jitter of seconds is not a move",
      classify(row(), row(due=LATER + timedelta(seconds=30)), now=NOW, tz=TZ), [])
check("moved into the past is not news", classify(row(), row(due=NOW - timedelta(hours=1)), now=NOW, tz=TZ), [])
check("nothing changed", classify(row(), row(), now=NOW, tz=TZ), [])

many = [{"kind": "new", "title": f"A{i}", "course": None, "detail": ""} for i in range(MAX_LINES + 3)]
text = render(many).splitlines()
check("the alert is capped", len(text), MAX_LINES + 2)
check("and says how many more", text[-1], "…and 3 more")
check("counts lead", text[0], f"📚 Canvas: {MAX_LINES + 3} new")

if FAILURES:
    print(f"FAIL ({len(FAILURES)})")
    for f in FAILURES:
        print("  -", f)
    raise SystemExit(1)
print("changes: new, graded, missing and moved classified; alerts capped")
