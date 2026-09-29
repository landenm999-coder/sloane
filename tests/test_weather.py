"""The weather skill against a stub Open-Meteo: FACTS, answers, nudges, outages.

No database and no network: the stub serves a fixed forecast on localhost.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _settings import isolated

from sloane.skills import Registry, SkillContext, load
from sloane.skills.weather import Weather, WeatherUnavailable, build, describe, parse_location

FAILURES: list[str] = []
DEN = ZoneInfo("America/Denver")


def check(label: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{label}\n     got: {got!r}\n    want: {want!r}")


def forecast() -> dict:
    """Three days, hourly. Rain at 4-5 PM today; snow at 7 AM tomorrow."""
    times, temps, chances, codes = [], [], [], []
    start = datetime(2026, 9, 24, 0, 0)
    for i in range(72):
        at = start + timedelta(hours=i)
        times.append(at.strftime("%Y-%m-%dT%H:%M"))
        temps.append(45 + (at.hour if at.hour < 15 else 30 - at.hour))
        chance, code = 0, 2
        if at.date() == start.date() and at.hour in (16, 17):
            chance, code = 70, 63
        if at.date() == (start + timedelta(days=1)).date() and at.hour == 7:
            chance, code = 80, 73
        chances.append(chance)
        codes.append(code)
    return {
        "timezone": "America/Denver",
        "current": {"time": "2026-09-24T09:00", "temperature_2m": 58.4, "apparent_temperature": 55.2,
                    "weather_code": 2, "wind_speed_10m": 7.4},
        "hourly": {"time": times, "temperature_2m": temps, "precipitation_probability": chances,
                   "weather_code": codes},
        "daily": {"time": ["2026-09-24", "2026-09-25", "2026-09-26"], "weather_code": [63, 73, 0],
                  "temperature_2m_max": [71.2, 48.0, 66.0], "temperature_2m_min": [44.6, 29.1, 40.0],
                  "precipitation_probability_max": [70, 80, 0]},
    }


class NoSessions:
    async def active_session(self, idle_minutes):
        return None


class Stub(BaseHTTPRequestHandler):
    calls: list[dict] = []
    status = 200

    def do_GET(self):  # noqa: N802
        Stub.calls.append({k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()})
        body = json.dumps(forecast()).encode() if Stub.status == 200 else b"{}"
        self.send_response(Stub.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # quiet
        pass


check("location parses", parse_location("39.5186, -104.7614"), (39.5186, -104.7614))
check("a bad location is none", [parse_location(x) for x in ["", "parker", "91,0", "1,2,3"]], [None] * 4)
check("codes become words", [describe(0), describe(73), describe(96), describe(1234), describe(None)],
      ["clear", "snow", "thunderstorms with hail", "unsettled", "unsettled"])
check("off without a location", build(SkillContext(store=None, config=isolated())), None)
check("off with a bad location", build(SkillContext(store=None, config=isolated(weather_location="nowhere"))), None)
check("load() skips it when unset", "weather" in load(SkillContext(store=None, config=isolated())).names, False)


async def main() -> None:
    server = HTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}/v1"
    now = {"at": datetime(2026, 9, 24, 9, 0, tzinfo=DEN)}
    config = isolated(timezone="America/Denver", weather_location="39.52,-104.76", weather_api_base=base)
    ctx = SkillContext(store=NoSessions(), config=config, clock=lambda: now["at"])
    skill = build(ctx)
    check("on with a location", isinstance(skill, Weather), True)
    reg = Registry([skill], ctx)

    facts = await skill.facts()
    check("FACTS are numbers and table words", facts, [
        "- WEATHER now: 58°F, feels like 55°F, partly cloudy",
        "- WEATHER today (Thu): high 71°F, low 45°F, rain, 70% chance of precipitation",
        "- WEATHER tomorrow (Fri): high 48°F, low 29°F, snow, 80% chance of precipitation",
        "- WEATHER rain likely from about 4 PM today (70%)",
    ])
    sent = Stub.calls[0]
    check("asks in his units and timezone", (sent["temperature_unit"], sent["timezone"], sent["latitude"]),
          ("fahrenheit", "America/Denver", "39.5200"))

    await skill.facts()
    panel = await skill.panel()
    check("cached: one fetch for three reads", len(Stub.calls), 1)
    check("the control room's curve: the next eight hours", [(h["at"][11:16], h["temp"], h["chance"]) for h in panel["hours"]],
          [("09:00", 54, 0), ("10:00", 55, 0), ("11:00", 56, 0), ("12:00", 57, 0), ("13:00", 58, 0),
           ("14:00", 59, 0), ("15:00", 60, 0), ("16:00", 59, 70)])
    check("and the sky in words", (panel["temp"], panel["sky"], panel["unit"]), (58.4, "partly cloudy", "°F"))

    check("what's the weather", (await reg.route("What's the weather?")).speech,
          "It's 58 and partly cloudy. Rain likely around 4 PM, high of 71.")
    check("is it going to rain", (await reg.route("is it going to rain today?")).speech,
          "It's 58 and partly cloudy. Rain likely around 4 PM, high of 71.")
    check("tomorrow", (await reg.route("will it snow tomorrow")).speech,
          "Tomorrow's high is 48, snow, with an 80 percent chance of snow.")
    check("jacket, with rain coming", (await reg.route("do I need a jacket?")).speech,
          "Yes: rain likely around 4 PM (70 percent).")
    check("/weather detail has the wet hours", " 4 PM  59°F  70%  rain" in (await reg.command("weather", "")).detail, True)
    for text in ["what's the weather in Denver like next week for the trip", "the weather was great yesterday"]:
        check(f"not ours: {text!r}", await reg.route(text), None)

    nudges = await skill.nudges()
    check("morning: rain later today", [(n.key, n.text) for n in nudges],
          [("weather:wet:2026-09-24", "🌧 Rain likely around 4 PM (70%). Take a jacket.")])
    now["at"] = datetime(2026, 9, 24, 16, 0, tzinfo=DEN)
    check("after 3 PM the rain nudge is too late to help", await skill.nudges(), [])
    now["at"] = datetime(2026, 9, 24, 20, 0, tzinfo=DEN)
    check("evening: snow tomorrow morning", [n.key for n in await skill.nudges()], ["weather:snow:2026-09-25"])

    # Callers that arrive together share one fetch.
    skill._cache = (skill._cache[0] - 25 * 60, skill._cache[1])
    before = len(Stub.calls)
    await asyncio.gather(skill.facts(), skill.panel(), skill.nudges(), skill.facts())
    check("concurrent readers share one fetch", len(Stub.calls) - before, 1)

    # Outages: a stale forecast is fine for a while, then it says so.
    Stub.status = 500
    skill._cache = (skill._cache[0] - 25 * 60, skill._cache[1])
    check("stale but recent: still answers", len(await skill.facts()) >= 3, True)
    skill._cache = (skill._cache[0] - 4 * 3600, skill._cache[1])
    try:
        await skill.facts()
    except WeatherUnavailable:
        pass
    else:
        FAILURES.append("a forecast over three hours old must not be used")
    check("the command says it can't", (await reg.command("weather", "")).speech,
          "I can't reach the weather right now.")
    tried = len(Stub.calls)
    for _ in range(3):
        try:
            await skill.facts()
        except WeatherUnavailable:
            pass
    check("after a failure it waits before asking again, so replies don't", len(Stub.calls), tried)
    lines, notes = await reg.facts()
    check("and FACTS gets a note, not a stale line", (lines, notes), ([], ["weather could not be read this turn"]))

    celsius = build(SkillContext(store=None, config=isolated(
        timezone="America/Denver", weather_location="39.52,-104.76", weather_api_base=base,
        weather_units="celsius"), clock=lambda: now["at"]))
    Stub.status = 200
    await celsius.facts()
    check("celsius asks for celsius", Stub.calls[-1]["temperature_unit"], "celsius")
    server.shutdown()


asyncio.run(main())

if FAILURES:
    print(f"FAIL ({len(FAILURES)})")
    for f in FAILURES:
        print("  -", f)
    raise SystemExit(1)
print("weather: FACTS, answers, nudges, caching and outages against a stub Open-Meteo")
