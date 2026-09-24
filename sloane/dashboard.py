"""The TV dashboard: GET /tv, one self-contained page for a screen on the wall.

Everything on it comes from SQL and the skills' panels, with no model, so it
keeps working when every provider is down. It refreshes itself every minute
and ticks its clock every second; it loads nothing from outside the page, so
it works on a tailnet with no internet and cannot leak a request anywhere.

Everything shown is escaped. Assignment titles and calendar events are
ingested text; on a web page an unescaped title is a script.

`collect()` reads; `render()` is a pure function of what it read, so the page
can be tested without a database.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sloane import views

log = logging.getLogger(__name__)

REFRESH_SECONDS = 60

# Response headers for the page: no framing, no outside requests, no sniffing.
HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def _clock(moment: datetime, zone: ZoneInfo) -> str:
    local = moment.astimezone(zone)
    hour = local.hour % 12 or 12
    return f"{hour}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"


async def collect(store, config, skills, now: datetime) -> dict[str, Any]:  # noqa: ANN001
    """Everything the page shows, read concurrently. A failed read is marked, not fatal."""
    zone = ZoneInfo(config.timezone)
    now = now.astimezone(zone)
    today = now.date()
    horizon = today + timedelta(days=3)
    reads = {
        "assignments": store.assignments_due(today, horizon),
        "shifts": store.shifts_between(today, today + timedelta(days=1)),
        "events": store.events_between(today, today + timedelta(days=1)),
        "overdue": store.overdue_assignments(),
        "courses": store.courses(),
        "reminders": store.upcoming_reminders(8),
        "commitments": store.open_commitments(),
        "alerts": store.open_alerts(),
    }
    if skills is not None:
        reads["panels"] = skills.panels()
    settled = await asyncio.gather(*reads.values(), return_exceptions=True)
    data: dict[str, Any] = {"now": now, "today": today, "tz": config.timezone, "unreadable": []}
    for name, result in zip(reads, settled):
        if isinstance(result, BaseException):
            log.warning("dashboard read %s failed: %s", name, result)
            data[name] = {} if name == "panels" else []
            data["unreadable"].append(name)
        else:
            data[name] = result
    data.setdefault("panels", {})
    return data


def _js(value: str) -> str:
    """A string as a JavaScript literal that cannot close the <script> it sits in."""
    return json.dumps(value).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _e(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _card(title: str, lines: list[str], *, cls: str = "", empty: str = "") -> str:
    body = "".join(f"<li>{_e(line)}</li>" for line in lines) if lines else (
        f'<li class="muted">{_e(empty)}</li>' if empty else "")
    if not body:
        return ""
    return f'<section class="card {cls}"><h2>{_e(title)}</h2><ul>{body}</ul></section>'


def _day(data: dict, day: date) -> list[str]:
    lines = views.day_lines(day, assignments=data["assignments"], shifts=data["shifts"],
                            events=data["events"], tz=data["tz"])
    return [line.removeprefix("• ") for line in lines]


def render(data: dict) -> str:
    """The page. Pure: the same data always renders the same HTML."""
    zone = ZoneInfo(data["tz"])
    now: datetime = data["now"]
    today: date = data["today"]
    tomorrow = today + timedelta(days=1)
    panels: dict[str, Any] = data.get("panels") or {}

    weather = panels.get("weather") if isinstance(panels.get("weather"), dict) else None
    weather_line = ""
    if weather and not weather.get("error") and weather.get("lines"):
        weather_line = f'<div class="weather">{_e(weather["lines"][0])}</div>'

    alerts = [f"⚠ {a['message']}" for a in data.get("alerts") or []]
    if data.get("unreadable"):
        alerts.append("Couldn't read: " + ", ".join(data["unreadable"]))
    banner = "".join(f'<div class="alert">{_e(a)}</div>' for a in alerts)

    due_soon = [a for a in data.get("assignments") or []
                if a.get("due_at") and a["due_at"].astimezone(zone).date() > today]
    due_lines = []
    for a in due_soon[:8]:
        local = a["due_at"].astimezone(zone)
        course = f" [{a['course']}]" if a.get("course") else ""
        due_lines.append(f"{local:%a} {_clock(local, zone)} — {a['title']}{course}")
    overdue = data.get("overdue") or []
    overdue_lines = [f"{o['title']}" + (f" [{o['course']}]" if o.get("course") else "") for o in overdue[:5]]
    if len(overdue) > 5:
        overdue_lines.append(f"…and {len(overdue) - 5} more")

    grades = sorted((c for c in data.get("courses") or [] if c.get("current_score") is not None),
                    key=lambda c: (c.get("period") is None, c.get("period") or 0))
    grade_lines = [f"{c['name']}: {c['current_score']:g}%" + (f" ({c['current_grade']})" if c.get("current_grade") else "")
                   for c in grades]

    reminder_lines = []
    for r in data.get("reminders") or []:
        local = r["due_at"].astimezone(zone)
        when = _clock(local, zone) if local.date() == today else f"{local:%a} {_clock(local, zone)}"
        reminder_lines.append(f"{when} — {r['text']}")
    promise_lines = [p["what"] + (f" (to {p['person']})" if p.get("person") else "")
                     for p in (data.get("commitments") or [])[:5]]

    cards = [
        _card("Today", _day(data, today), cls="wide", empty="Nothing scheduled."),
        _card("Tomorrow", _day(data, tomorrow), empty="Nothing scheduled."),
        _card("Overdue", overdue_lines, cls="warn"),
        _card("Due soon", due_lines, empty="Nothing in the next three days."),
        _card("Reminders", reminder_lines),
        _card("Promises", promise_lines),
        _card("Grades", grade_lines),
    ]
    for name, panel in panels.items():
        if name == "weather" or not isinstance(panel, dict):
            continue
        if panel.get("error"):
            cards.append(_card(name.capitalize(), [f"{name} is unavailable"], cls="muted-card"))
            continue
        lines = [str(line) for line in panel.get("lines") or []]
        cards.append(_card(str(panel.get("title") or name.capitalize()), lines))

    weekday = f"{now:%A}"
    long_date = f"{now:%B} {now.day}"
    body = "".join(c for c in cards if c)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="{REFRESH_SECONDS}">
<title>Sloane</title>
<style>
:root {{
  --bg: #0d1117; --panel: #161b22; --line: #30363d; --text: #e6edf3; --muted: #8b949e;
  --accent: #7ee2b8; --warn: #f0883e; --alert: #da3633;
}}
* {{ box-sizing: border-box; }}
html, body {{ margin: 0; background: var(--bg); color: var(--text);
  font: 400 clamp(15px, 1.05vw, 24px)/1.3 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }}
header {{ display: flex; align-items: baseline; gap: 1.2em; padding: 1.1rem 1.6rem .6rem; flex-wrap: wrap; }}
.clock {{ font-size: 3.6em; font-weight: 600; letter-spacing: -.02em; font-variant-numeric: tabular-nums; }}
.date {{ font-size: 1.4em; color: var(--muted); }}
.weather {{ font-size: 1.4em; margin-left: auto; }}
.alert {{ margin: 0 1.6rem .6rem; padding: .5em .8em; border-radius: .5em; background: var(--alert); font-weight: 600; }}
main {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(17em, 1fr)); gap: .9rem; padding: .4rem 1.6rem 1.6rem; }}
.card {{ background: var(--panel); border: 1px solid var(--line); border-radius: .8em; padding: .8em 1em; min-width: 0; }}
.card.wide {{ grid-column: span 2; }}
.card.warn h2 {{ color: var(--warn); }}
.muted-card {{ opacity: .6; }}
h2 {{ margin: 0 0 .45em; font-size: .85em; font-weight: 600; text-transform: uppercase; letter-spacing: .08em; color: var(--accent); }}
ul {{ list-style: none; margin: 0; padding: 0; }}
li {{ padding: .22em 0; border-top: 1px solid var(--line); overflow-wrap: anywhere; }}
li:first-child {{ border-top: 0; }}
.muted {{ color: var(--muted); }}
@media (max-width: 40em) {{ .card.wide {{ grid-column: auto; }} .weather {{ margin-left: 0; }} }}
</style>
</head>
<body>
<header>
  <div class="clock" id="clock">{_e(_clock(now, zone))}</div>
  <div class="date">{_e(weekday)}, {_e(long_date)}</div>
  {weather_line}
</header>
{banner}
<main>{body}</main>
<script>
(function () {{
  var el = document.getElementById("clock");
  function tick() {{
    var d = new Date(new Date().toLocaleString("en-US", {{ timeZone: {_js(data["tz"])} }}));
    var h = d.getHours() % 12 || 12, m = ("0" + d.getMinutes()).slice(-2);
    el.textContent = h + ":" + m + (d.getHours() < 12 ? " AM" : " PM");
  }}
  setInterval(tick, 1000);
}})();
</script>
</body>
</html>
"""
