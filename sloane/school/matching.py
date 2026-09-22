"""Matching an upstream course name to a seeded one.

The seed knows the period and the teacher; Canvas knows the id and its own name
for the class. Joining them is what lets "what's due Friday" answer "Stat
p. 214, Austin, period 2" instead of echoing whatever the course is called
upstream.

The matching is deliberately conservative. A wrong match is worse than none: it
would attribute work to the wrong teacher and period, and she states FACTS
verbatim. So this matches only when every word of the shorter name is a prefix
of a word in the longer one -- "Stat Reasoning" against "Statistical Reasoning
- P2" -- and refuses to choose when two candidates both qualify.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

_WORD = re.compile(r"[a-z0-9]+")

# Section and period markers that carry no identity: "P2", "Period 3", "Sec 1",
# "2026", "S1", and single letters left over from splitting.
_NOISE = re.compile(r"^(p|per|period|sec|section|s|sem|semester|hr|hour|block)?\d*$")

# Minimum prefix before "stat" may match "statistical". Below this, "a" would
# match everything.
MIN_PREFIX = 3


def tokens(name: str) -> list[str]:
    """Identity-bearing lowercase words, with section noise dropped."""
    found = _WORD.findall((name or "").lower())
    return [w for w in found if not _NOISE.fullmatch(w)]


def is_match(a: str, b: str) -> bool:
    """True when the shorter name's words all prefix the longer name's words."""
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return False
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)

    remaining = list(long_)
    for word in short:
        hit = next(
            (
                other
                for other in remaining
                if other == word
                or (len(word) >= MIN_PREFIX and other.startswith(word))
                or (len(other) >= MIN_PREFIX and word.startswith(other))
            ),
            None,
        )
        if hit is None:
            return False
        remaining.remove(hit)
    return True


def best_match(name: str, candidates: Sequence[dict[str, Any]]) -> dict | None:
    """The one candidate matching `name`, or None if none or more than one.

    Returning None on ambiguity is the point. Two plausible courses means the
    seed needs disambiguating, not that we should pick the first row.
    """
    hits = [c for c in candidates if is_match(name, str(c.get("name", "")))]
    if len(hits) != 1:
        return None
    return hits[0]
