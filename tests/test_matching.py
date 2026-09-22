"""Course-name matching. No database, no network.

A wrong match puts another teacher's name on Landen's homework and she states
FACTS verbatim, so these cases are about what the matcher *refuses* as much as
what it accepts.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sloane.school.matching import MIN_PREFIX, best_match, is_match, tokens

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{label}\n     got: {got!r}\n    want: {want!r}")


# --- the real seeded names against plausible upstream ones ------------------
for seeded, upstream, want in [
    ("Stat Reasoning", "Statistical Reasoning - P2", True),
    ("Physics", "Physics", True),
    ("Physics", "AP Physics C: Mechanics", True),       # still his physics class
    ("Colorado History", "Colorado History (Period 3)", True),
    ("Jewelry I", "Jewelry 1 - S1", True),
    ("American Tapestry", "American Tapestry 2026", True),
    # ...and what it must refuse
    ("Physics", "Chemistry", False),
    ("Stat Reasoning", "US History", False),
    ("Off period", "Statistical Reasoning", False),
    ("Jewelry I", "Ceramics I", False),
]:
    check(f"{seeded!r} vs {upstream!r}", is_match(seeded, upstream), want)

# --- section noise is not identity ------------------------------------------
check("period markers dropped", tokens("Statistical Reasoning - P2"),
      ["statistical", "reasoning"])
check("year and section dropped", tokens("Physics S1 Period 6"), ["physics"])
check("empty name yields nothing", tokens(""), [])
check("a name that is only noise never matches", is_match("P2", "Period 3"), False)

# --- short abbreviations must not match loosely -----------------------------
check(
    f"below the {MIN_PREFIX}-character floor, an abbreviation is refused",
    is_match("CO History", "Colorado History"), False,
)
check(
    "which is why it cannot wrongly claim a different course either",
    is_match("CO History", "Computer Science History"), False,
)

# --- ambiguity is refused, not guessed --------------------------------------
two = [{"id": 1, "name": "Physics"}, {"id": 2, "name": "Physics Lab"}]
check("two plausible matches means no match", best_match("Physics", two), None)
one = [{"id": 1, "name": "Statistical Reasoning"}, {"id": 2, "name": "Ceramics"}]
check("one clear match is taken", best_match("Stat Reasoning", one)["id"], 1)
check("no candidates is not an error", best_match("Anything", []), None)

if FAILURES:
    print(f"FAIL ({len(FAILURES)})")
    for f in FAILURES:
        print("  -", f)
    raise SystemExit(1)
print("matching: accepts the real names, refuses the ambiguous ones")
