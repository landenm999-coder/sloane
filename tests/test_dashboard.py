"""The TV dashboard: what it shows, that it escapes everything, and that it degrades.

The unit half renders fixed data. The integration half collects from the real
store (read-only: it writes nothing).
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _settings import isolated

from sloane import dashboard

FAILURES: list[str] = []
DEN = ZoneInfo("America/Denver")
NOW = datetime(2026, 9, 24, 16, 5, tzinfo=DEN)


def check(label: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{label}\n     got: {got!r}\n    want: {want!r}")


def data(**over) -> dict:
    base = {
        "now": NOW, "today": NOW.date(), "tz": "America/Denver", "unreadable": [],
        "assignments": [
            {"title": "Lab <script>alert(1)</script>", "due_at": datetime(2026, 9, 24, 23, 59, tzinfo=DEN),
             "course": "Physics", "all_day": False},
            {"title": "Essay draft", "due_at": datetime(2026, 9, 26, 8, 0, tzinfo=DEN), "course": "English"},
        ],
        "shifts": [{"starts_at": datetime(2026, 9, 24, 15, tzinfo=DEN), "ends_at": datetime(2026, 9, 24, 19, tzinfo=DEN)}],
        "events": [],
        "overdue": [{"title": "Old worksheet", "course": "Stats"}],
        "courses": [{"name": "Physics", "period": 6, "current_score": 91.5, "current_grade": "A-"},
                    {"name": "Jewelry", "period": 1, "current_score": None}],
        "reminders": [{"text": "call Keegan", "due_at": datetime(2026, 9, 24, 19, 30, tzinfo=DEN)}],
        "commitments": [{"what": "send the outline", "person": "Keegan"}],
        "alerts": [],
        "panels": {
            "weather": {"title": "Weather", "lines": ["⛅ 58°F partly cloudy (feels 55°F)"]},
            "lists": {"title": "Lists", "lines": ["Grocery: milk, eggs"]},
            "broken": {"error": "unavailable"},
        },
    }
    base.update(over)
    return base


page = dashboard.render(data())
check("a doctype and a title", page.startswith("<!doctype html>") and "<title>Sloane</title>" in page, True)
check("an ingested title cannot run", "<script>alert(1)</script>" in page, False)
check("it is shown escaped", "Lab &lt;script&gt;alert(1)&lt;/script&gt;" in page, True)
check("today's shift", "WORK 3:00 PM–7:00 PM" in page, True)
check("due soon excludes today", page.count("Essay draft"), 1)
check("overdue is its own card", "Old worksheet [Stats]" in page, True)
check("grades with a score only", ("Physics: 91.5% (A-)" in page, "Jewelry:" in page), (True, False))
check("reminders", "7:30 PM — call Keegan" in page, True)
check("promises", "send the outline (to Keegan)" in page, True)
check("weather rides in the header", '<div class="weather">⛅ 58°F partly cloudy (feels 55°F)</div>' in page, True)
check("a skill panel is a card", "<h2>Lists</h2>" in page and "Grocery: milk, eggs" in page, True)
check("a broken skill says so", "broken is unavailable" in page, True)
check("the clock starts at now", '<div class="clock" id="clock">4:05 PM</div>' in page, True)
check("the timezone reaches the script as a JS string", 'timeZone: "America/Denver"' in page, True)
check("it refreshes itself", f'content="{dashboard.REFRESH_SECONDS}"' in page, True)
check("no outside requests", ("http://" in page, "https://" in page, "src=" in page), (False, False, False))
check("CSP forbids outside loads", "default-src 'none'" in dashboard.HEADERS["Content-Security-Policy"], True)

hostile = dashboard.render(data(alerts=[{"message": "<img src=x onerror=alert(1)>"}], unreadable=["overdue"],
                                panels={"x": {"title": "<b>t</b>", "lines": ["</script><script>alert(2)"]}}))
check("alerts escaped", "<img src=x" in hostile, False)
check("a failed read is a banner", "Couldn&#x27;t read: overdue" in hostile, True)
check("panel titles and lines escaped", ("<b>t</b>" in hostile, "</script><script>alert(2)" in hostile), (False, False))
empty = dashboard.render(data(assignments=[], shifts=[], overdue=[], courses=[], reminders=[], commitments=[], panels={}))
check("an empty day says so", "Nothing scheduled." in empty and "Nothing in the next three days." in empty, True)


async def integration() -> None:
    from sloane.memory.store import Store
    from sloane.skills import Registry, SkillContext
    from sloane.skills.lists import Lists

    config = isolated(database_url=os.environ["DATABASE_URL"], timezone="America/Denver")
    async with Store(config) as store:
        ctx = SkillContext(store=store, config=config)
        collected = await dashboard.collect(store, config, Registry([Lists(ctx)], ctx), NOW)
        check("every read works against the real schema", collected["unreadable"], [])
        check("today is local", collected["today"], date(2026, 9, 24))
        html = dashboard.render(collected)
        check("and renders", "<main>" in html, True)

        class Down:
            def __getattr__(self, name):
                async def fail(*a, **k):
                    raise RuntimeError("database is down")
                return fail

        down = await dashboard.collect(Down(), config, None, NOW)
        check("a dead database is a banner, not a 500", sorted(down["unreadable"])[:2], ["alerts", "assignments"])
        check("and still renders", "Couldn&#x27;t read" in dashboard.render(down), True)


if os.environ.get("DATABASE_URL"):
    asyncio.run(integration())

if FAILURES:
    print(f"FAIL ({len(FAILURES)})")
    for f in FAILURES:
        print("  -", f)
    raise SystemExit(1)
print("dashboard: every card, everything escaped, weather header, degraded reads")
