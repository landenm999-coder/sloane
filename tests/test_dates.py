"""Dates from words, by rules: the table, the edges, and what is refused."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sloane.dates import find, remove, spoken, until

FAILURES: list[str] = []
# A Thursday.
TODAY = date(2026, 9, 24)


def check(label: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{label}\n     got: {got!r}\n    want: {want!r}")


TABLE = [
    ("2027-05-22", date(2027, 5, 22)),
    ("graduation 5/22", date(2027, 5, 22)),
    ("5/22/27", date(2027, 5, 22)),
    ("10/3/2026", date(2026, 10, 3)),
    ("may 22", date(2027, 5, 22)),          # passed this year: next year's
    ("May 22nd", date(2027, 5, 22)),
    ("Sept. 30", date(2026, 9, 30)),
    ("oct 3, 2027", date(2027, 10, 3)),
    ("22 may", date(2027, 5, 22)),
    ("the 3rd of december", date(2026, 12, 3)),
    ("september 24", TODAY),                 # today counts as not passed
    ("the 30th", date(2026, 9, 30)),
    ("the 2nd", date(2026, 10, 2)),         # passed this month: next month's
    ("the 31st", date(2026, 10, 31)),       # September has no 31st
    ("in 3 days", date(2026, 9, 27)),
    ("in two weeks", date(2026, 10, 8)),
    ("in a month", date(2026, 10, 24)),
    ("in 1 year", date(2027, 9, 24)),
    ("today", TODAY),
    ("tomorrow", date(2026, 9, 25)),
    ("day after tomorrow", date(2026, 9, 26)),
    ("friday", date(2026, 9, 25)),
    ("on fri", date(2026, 9, 25)),
    ("thursday", TODAY),
    ("next thursday", date(2026, 10, 1)),
    ("next friday", date(2026, 9, 25)),
    ("on sat", date(2026, 9, 26)),
    ("by sun", date(2026, 9, 27)),
]
for text, want in TABLE:
    found = find(text, TODAY)
    check(f"find({text!r})", found.day if found else None, want)

check("a leap day in a common year is the next leap year",
      find("feb 29", TODAY).day, date(2028, 2, 29))
check("an impossible date is no date", find("feb 30", TODAY), None)
check("month 13 is no date", find("13/5", TODAY), None)
check("no date at all", find("call keegan", TODAY), None)
check("'sun' alone is a word, not Sunday", find("sun valley trip", TODAY), None)
check("'sat' alone is a word", find("sat down with the essay", TODAY), None)
check("a year was given", find("oct 3, 2027", TODAY).year_given, True)
check("a year was not given", find("oct 3", TODAY).year_given, False)

# The past direction, for "spent 12 yesterday" and "met them friday".
check("yesterday", find("yesterday", TODAY, future=False).day, date(2026, 9, 23))
check("last friday, looking back", find("friday", TODAY, future=False).day, date(2026, 9, 18))
check("may 22, looking back", find("may 22", TODAY, future=False).day, date(2026, 5, 22))
check("the 30th, looking back", find("the 30th", TODAY, future=False).day, date(2026, 8, 30))

# What's left once the date is taken out.
REMOVE = [
    ("graduation is on may 22", "graduation"),
    ("DECA districts dec 3", "DECA districts"),
    ("send mockups by friday", "send mockups"),
    ("prom: 4/18", "prom"),
    ("follow up in 2 weeks about the quote", "follow up about the quote"),
]
for text, want in REMOVE:
    check(f"remove({text!r})", remove(text, find(text, TODAY)), want)

check("spoken today", spoken(TODAY, TODAY), "today")
check("spoken this week", spoken(date(2026, 9, 27), TODAY), "Sunday")
check("spoken later", spoken(date(2026, 10, 3), TODAY), "Sat Oct 3")
check("spoken next year", spoken(date(2027, 9, 23), TODAY), "Thu Sep 23, 2027")
check("until", [until(date(2026, 9, 24), TODAY), until(date(2026, 9, 25), TODAY),
                until(date(2026, 10, 3), TODAY), until(date(2026, 9, 20), TODAY)],
      ["today", "tomorrow", "in 9 days", "4 days ago"])

if FAILURES:
    print(f"FAIL ({len(FAILURES)})")
    for f in FAILURES:
        print("  -", f)
    raise SystemExit(1)
print(f"dates: {len(TABLE) + len(REMOVE)} phrasings, the edges, and the refusals")
