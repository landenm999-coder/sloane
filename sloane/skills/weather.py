"""Weather, from Open-Meteo: free, no key, no signup.

    /weather  ·  /weather tomorrow
    "what's the weather?"  ·  "is it going to rain today?"  ·  "do I need a jacket?"

Off unless WEATHER_LOCATION is set ("39.52,-104.76" for Parker). The numbers
go in FACTS -- temperatures and chances are structured values from an API,
which is what the skill contract allows there -- and the words for the sky
come from a fixed table of WMO codes, never from free text.

Two nudges, through the heartbeat, each once a day at most:

* rain, snow or a thunderstorm likely later today (said in the morning and
  early afternoon, while there is still time to grab a jacket before work);
* snow likely tomorrow morning (said in the evening, so he can leave early).

A forecast is fetched at most every 20 minutes. If Open-Meteo is down, the
last forecast is used for up to three hours, and after that the skill says it
cannot read the weather rather than repeating a stale one. A failed fetch is
not retried for five minutes: every reply waits for FACTS, so an outage must
cost one slow reply, not all of them.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time as clock
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from sloane.skills import Answer, Nudge, Skill, SkillContext

log = logging.getLogger(__name__)

FRESH_SECONDS = 20 * 60
STALE_OK_SECONDS = 3 * 3600
# After a failed fetch, wait this long before trying again. Every reply reads
# FACTS, and FACTS waits for every skill: an outage must cost one slow reply,
# not every reply for an afternoon.
RETRY_AFTER_SECONDS = 5 * 60
TIMEOUT_SECONDS = 6.0
# A chance at or above this is "likely".
LIKELY = 60

# WMO weather interpretation codes, as Open-Meteo documents them.
CODES: dict[int, str] = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "freezing drizzle", 57: "heavy freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    66: "freezing rain", 67: "heavy freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "rain showers", 81: "heavy rain showers", 82: "violent rain showers",
    85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorms", 96: "thunderstorms with hail", 99: "thunderstorms with heavy hail",
}
SNOW = {71, 73, 75, 77, 85, 86}
STORM = {95, 96, 99}
WET = {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82} | SNOW | STORM
ICONS = {"clear": "☀️", "mostly clear": "🌤", "partly cloudy": "⛅", "overcast": "☁️"}

_ASK = re.compile(
    r"^\s*(?:(?:hey\s+)?(?:what'?s|whats|what\s+is|how'?s|how\s+is)\s+(?:the\s+)?(?:weather|forecast)(?:\s+like)?"
    r"|weather|forecast"
    r"|(?:is\s+it|will\s+it|is\s+it\s+going\s+to|it\s+gonna|is\s+it\s+gonna)\s+(?:be\s+)?(?:rain|snow|storm|cold|hot|rainy|snowy)(?:ing)?"
    r"|do\s+i\s+need\s+(?:a\s+|an\s+)?(?:jacket|coat|umbrella|hoodie|sweatshirt))"
    r"(?:\s+(?P<when>today|tonight|tomorrow|this\s+(?:morning|afternoon|evening)))?\s*[?.!]*\s*$",
    re.I,
)


class WeatherUnavailable(RuntimeError):
    pass


def parse_location(raw: str) -> tuple[float, float] | None:
    """'39.52,-104.76' -> (39.52, -104.76). None if it is not a real place."""
    parts = [p.strip() for p in (raw or "").split(",")]
    if len(parts) != 2:
        return None
    try:
        lat, lon = float(parts[0]), float(parts[1])
    except ValueError:
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


def describe(code: Any) -> str:
    try:
        return CODES.get(int(code), "unsettled")
    except (TypeError, ValueError):
        return "unsettled"


@dataclass(frozen=True)
class Hour:
    at: datetime
    temp: float | None
    chance: int
    code: int


@dataclass(frozen=True)
class Day:
    on: date
    high: float | None
    low: float | None
    chance: int
    code: int


@dataclass(frozen=True)
class Forecast:
    temp: float | None
    feels: float | None
    code: int
    wind: float | None
    days: tuple[Day, ...]
    hours: tuple[Hour, ...]

    def day(self, on: date) -> Day | None:
        return next((d for d in self.days if d.on == on), None)


def _num(values: list, i: int) -> float | None:
    try:
        value = values[i]
    except (IndexError, TypeError):
        return None
    return float(value) if isinstance(value, (int, float)) else None


def parse_forecast(body: dict, tz: str) -> Forecast:
    """Open-Meteo's JSON (requested with timezone=<tz>) into numbers we trust."""
    zone = ZoneInfo(tz)
    current = body.get("current") or {}
    daily = body.get("daily") or {}
    hourly = body.get("hourly") or {}
    days = []
    for i, raw in enumerate(daily.get("time") or []):
        try:
            on = date.fromisoformat(str(raw))
        except ValueError:
            continue
        days.append(Day(on, _num(daily.get("temperature_2m_max"), i), _num(daily.get("temperature_2m_min"), i),
                        int(_num(daily.get("precipitation_probability_max"), i) or 0),
                        int(_num(daily.get("weather_code"), i) or 0)))
    hours = []
    for i, raw in enumerate(hourly.get("time") or []):
        try:
            at = datetime.fromisoformat(str(raw)).replace(tzinfo=zone)
        except ValueError:
            continue
        hours.append(Hour(at, _num(hourly.get("temperature_2m"), i),
                          int(_num(hourly.get("precipitation_probability"), i) or 0),
                          int(_num(hourly.get("weather_code"), i) or 0)))
    if not days and current.get("temperature_2m") is None:
        raise WeatherUnavailable("the forecast came back empty")
    return Forecast(
        temp=_num([current.get("temperature_2m")], 0),
        feels=_num([current.get("apparent_temperature")], 0),
        code=int(_num([current.get("weather_code")], 0) or 0),
        wind=_num([current.get("wind_speed_10m")], 0),
        days=tuple(days),
        hours=tuple(hours),
    )


def _clock(at: datetime) -> str:
    hour = at.hour % 12 or 12
    return f"{hour} {'AM' if at.hour < 12 else 'PM'}"


class Weather(Skill):
    name = "weather"
    help = ("`/weather` · `/weather tomorrow` — the forecast (or ask \"is it going to rain?\")",)
    commands = frozenset({"weather"})

    def __init__(self, ctx: SkillContext, location: tuple[float, float]) -> None:
        super().__init__(ctx)
        self.location = location
        self.celsius = ctx.config.weather_units.strip().lower().startswith("c")
        self._cache: tuple[float, Forecast] | None = None
        self._retry_at = 0.0
        self._lock = asyncio.Lock()

    # -- units ---------------------------------------------------------------------

    @property
    def deg(self) -> str:
        return "°C" if self.celsius else "°F"

    def t(self, value: float | None) -> str:
        return "?" if value is None else f"{round(value)}{self.deg}"

    @property
    def cold(self) -> float:
        return 5 if self.celsius else 41

    # -- fetching ------------------------------------------------------------------

    async def forecast(self) -> Forecast:
        if self._cache and clock.monotonic() - self._cache[0] < FRESH_SECONDS:
            return self._cache[1]
        # FACTS, the TV and the heartbeat can all ask at once: one fetch serves them.
        async with self._lock:
            return await self._fetch()

    async def _fetch(self) -> Forecast:
        now = clock.monotonic()
        if self._cache and now - self._cache[0] < FRESH_SECONDS:
            return self._cache[1]  # fetched while this caller waited for the lock
        if now < self._retry_at:
            if self._cache and now - self._cache[0] < STALE_OK_SECONDS:
                return self._cache[1]
            raise WeatherUnavailable("Open-Meteo failed a moment ago; trying again in a few minutes")
        lat, lon = self.location
        params = {
            "latitude": f"{lat:.4f}", "longitude": f"{lon:.4f}",
            "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
            "hourly": "temperature_2m,precipitation_probability,weather_code",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "temperature_unit": "celsius" if self.celsius else "fahrenheit",
            "wind_speed_unit": "kmh" if self.celsius else "mph",
            "timezone": self.ctx.config.timezone,
            "forecast_days": 3,
        }
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
                response = await client.get(f"{self.ctx.config.weather_api_base.rstrip('/')}/forecast", params=params)
            if response.status_code != 200:
                raise WeatherUnavailable(f"Open-Meteo answered HTTP {response.status_code}")
            fresh = parse_forecast(response.json(), self.ctx.config.timezone)
        except (httpx.HTTPError, ValueError, WeatherUnavailable) as exc:
            self._retry_at = now + RETRY_AFTER_SECONDS
            if self._cache and now - self._cache[0] < STALE_OK_SECONDS:
                log.warning("weather fetch failed, using the last forecast: %s", exc)
                return self._cache[1]
            raise WeatherUnavailable(str(exc)) from exc
        self._cache = (now, fresh)
        self._retry_at = 0.0
        return fresh

    # -- words -------------------------------------------------------------------------

    def _next_wet(self, f: Forecast, start: datetime, end: datetime) -> Hour | None:
        """The first hour in [start, end) where rain, snow or a storm is likely."""
        for h in f.hours:
            if start <= h.at < end and h.chance >= LIKELY:
                return h
        return None

    def _day_words(self, d: Day) -> str:
        chance = f", {d.chance}% chance of precipitation" if d.chance else ""
        return f"high {self.t(d.high)}, low {self.t(d.low)}, {describe(d.code)}{chance}"

    def _spoken_day(self, label: str, d: Day) -> str:
        article = "an" if str(d.chance).startswith("8") or d.chance in (11, 18) else "a"
        chance = (f", with {article} {d.chance} percent chance of {'snow' if d.code in SNOW else 'rain'}"
                  if d.chance >= 20 else "")
        return f"{label} high is {round(d.high) if d.high is not None else 'unknown'}, {describe(d.code)}{chance}."

    async def report(self, when: str = "") -> Answer:
        f = await self.forecast()
        now = self.ctx.now()
        today = now.date()
        when = (when or "").lower()
        if when == "tomorrow":
            d = f.day(today + timedelta(days=1))
            if d is None:
                return Answer("I don't have tomorrow's forecast.")
            morning = [h for h in f.hours if h.at.date() == d.on and 7 <= h.at.hour <= 9]
            speech = self._spoken_day("Tomorrow's", d)
            detail = f"Tomorrow: {self._day_words(d)}"
            if morning and morning[0].temp is not None:
                detail += f"\nMorning (7 AM): {self.t(morning[0].temp)}"
            return Answer(speech, detail)

        d = f.day(today)
        speech = f"It's {round(f.temp) if f.temp is not None else 'unknown'} and {describe(f.code)}."
        if d is not None:
            wet = self._next_wet(f, now, now.replace(hour=23, minute=59))
            if wet is not None:
                kind = "snow" if wet.code in SNOW else ("storms" if wet.code in STORM else "rain")
                speech += f" {kind.capitalize()} likely around {_clock(wet.at)}, high of {round(d.high) if d.high is not None else '?'}."
            else:
                speech += " " + self._spoken_day("Today's", d)
        lines = [f"Now: {self.t(f.temp)} (feels {self.t(f.feels)}), {describe(f.code)}"
                 + (f", wind {round(f.wind)} {'km/h' if self.celsius else 'mph'}" if f.wind is not None else "")]
        for label, offset in (("Today", 0), ("Tomorrow", 1)):
            day = f.day(today + timedelta(days=offset))
            if day is not None:
                lines.append(f"{label}: {self._day_words(day)}")
        upcoming = [h for h in f.hours if now - timedelta(minutes=59) < h.at <= now + timedelta(hours=12)]
        if upcoming:
            lines.append("")
            # Every other hour, plus any hour worth a look.
            shown = [h for i, h in enumerate(upcoming) if i % 2 == 0 or h.chance >= 30]
            lines += [f"{_clock(h.at):>5}  {self.t(h.temp)}  {h.chance}%  {describe(h.code)}" for h in shown]
        return Answer(speech, "\n".join(lines))

    async def _jacket(self) -> Answer:
        f = await self.forecast()
        now = self.ctx.now()
        d = f.day(now.date())
        wet = self._next_wet(f, now, now.replace(hour=23, minute=59))
        low = min([h.temp for h in f.hours if now <= h.at <= now.replace(hour=22) and h.temp is not None] or
                  [f.temp if f.temp is not None else 99])
        if wet is not None:
            kind = "snow" if wet.code in SNOW else ("storms" if wet.code in STORM else "rain")
            return Answer(f"Yes: {kind} likely around {_clock(wet.at)} ({wet.chance} percent).")
        if low <= self.cold:
            return Answer(f"Yes, it drops to {round(low)} degrees later.")
        high = f" High of {round(d.high)}." if d is not None and d.high is not None else ""
        return Answer(f"Probably not: no rain likely, and it stays above {round(low)} degrees.{high}")

    # -- the skill contract ----------------------------------------------------------

    async def command(self, name: str, rest: str) -> Answer | None:
        try:
            return await self.report("tomorrow" if "tomorrow" in rest.lower() else "")
        except WeatherUnavailable as exc:
            return Answer("I can't reach the weather right now.", str(exc)[:200])

    async def match(self, text: str) -> Answer | None:
        found = _ASK.match(text.replace("’", "'"))
        if found is None:
            return None
        try:
            if re.search(r"\b(?:need|jacket|coat|umbrella|hoodie|sweatshirt)\b", text, re.I):
                return await self._jacket()
            return await self.report("tomorrow" if (found["when"] or "").lower() == "tomorrow" else "")
        except WeatherUnavailable as exc:
            return Answer("I can't reach the weather right now.", str(exc)[:200])

    async def facts(self) -> list[str]:
        f = await self.forecast()  # raising here becomes a NOTE, not an empty line
        today = self.ctx.now().date()
        lines = [f"- WEATHER now: {self.t(f.temp)}, feels like {self.t(f.feels)}, {describe(f.code)}"]
        for label, offset in (("today", 0), ("tomorrow", 1)):
            d = f.day(today + timedelta(days=offset))
            if d is not None:
                lines.append(f"- WEATHER {label} ({d.on:%a}): {self._day_words(d)}")
        now = self.ctx.now()
        wet = self._next_wet(f, now, now.replace(hour=23, minute=59))
        if wet is not None:
            lines.append(f"- WEATHER {describe(wet.code)} likely from about {_clock(wet.at)} today ({wet.chance}%)")
        return lines

    async def panel(self) -> dict | None:
        f = await self.forecast()
        today = self.ctx.now().date()
        lines = [f"{ICONS.get(describe(f.code), '')} {self.t(f.temp)} {describe(f.code)} (feels {self.t(f.feels)})".strip()]
        for label, offset in (("Today", 0), ("Tomorrow", 1)):
            d = f.day(today + timedelta(days=offset))
            if d is not None:
                lines.append(f"{label}: {self.t(d.high)} / {self.t(d.low)}, {describe(d.code)}"
                             + (f", {d.chance}%" if d.chance else ""))
        # The next eight hours from this one, for the control room's curve.
        hour = self.ctx.now().replace(minute=0, second=0, microsecond=0)
        hours = [{"at": h.at.isoformat(), "temp": h.temp, "chance": h.chance}
                 for h in f.hours if h.at >= hour and h.temp is not None][:8]
        day = f.day(today)
        return {"title": "Weather", "lines": lines, "temp": f.temp, "code": f.code,
                "sky": describe(f.code), "unit": self.deg, "hours": hours, "feels": f.feels,
                "high": day.high if day is not None else None, "low": day.low if day is not None else None}

    async def nudges(self) -> list[Nudge]:
        now = self.ctx.now()
        out: list[Nudge] = []
        minutes = now.hour * 60 + now.minute
        if 6 * 60 + 30 <= minutes < 15 * 60:
            f = await self.forecast()
            wet = self._next_wet(f, now + timedelta(minutes=30), now.replace(hour=21, minute=0))
            if wet is not None:
                if wet.code in SNOW:
                    text = f"🌨 Snow likely around {_clock(wet.at)} ({wet.chance}%). Leave extra time."
                elif wet.code in STORM:
                    text = f"⛈ Thunderstorms likely around {_clock(wet.at)} ({wet.chance}%)."
                else:
                    text = f"🌧 Rain likely around {_clock(wet.at)} ({wet.chance}%). Take a jacket."
                out.append(Nudge(f"weather:wet:{now.date()}", text))
        elif 19 * 60 <= minutes < 22 * 60:
            f = await self.forecast()
            tomorrow = now.date() + timedelta(days=1)
            morning = [h for h in f.hours if h.at.date() == tomorrow and 5 <= h.at.hour <= 9
                       and h.code in SNOW and h.chance >= LIKELY]
            if morning:
                h = morning[0]
                out.append(Nudge(f"weather:snow:{tomorrow}",
                                 f"🌨 Snow likely tomorrow morning around {_clock(h.at)} ({h.chance}%). Leave early."))
        return out


def build(ctx: SkillContext) -> Skill | None:
    location = parse_location(ctx.config.weather_location)
    if location is None:
        if ctx.config.weather_location.strip():
            log.warning("WEATHER_LOCATION %r is not 'latitude,longitude'; weather is off",
                        ctx.config.weather_location)
        return None
    return Weather(ctx, location)
