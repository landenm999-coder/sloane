"""One pass over every upstream source, into tier 4.

Ordering matters: courses first, so assignments have something to hang off and
"what's due Friday" can answer with a period and a teacher rather than a bare
name.

Every source is independent. One failing must not cost the others -- a Canvas
outage should still leave the calendar and the generated shifts correct, and
the report says plainly which parts are stale rather than letting her answer
from a half-synced table as though it were complete.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sloane.config import Settings
from sloane.memory.store import Store
from sloane.school import SchoolError
from sloane.school.calendar import fetch as fetch_ics, parse as parse_ics
from sloane.school.canvas import CanvasClient
from sloane.school.matching import best_match
from sloane.school.shifts import planned_shifts

log = logging.getLogger(__name__)


@dataclass
class SourceResult:
    name: str
    ok: bool
    written: int = 0
    detail: str = ""


@dataclass
class SyncReport:
    sources: list[SourceResult] = field(default_factory=list)
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def ok(self) -> bool:
        return all(s.ok for s in self.sources)

    @property
    def written(self) -> int:
        return sum(s.written for s in self.sources)

    @property
    def failures(self) -> list[SourceResult]:
        return [s for s in self.sources if not s.ok]

    def speech(self) -> str:
        """One or two sentences, for the reply contract."""
        if not self.sources:
            return "Nothing is configured to sync yet."
        if self.ok:
            return f"Synced {self.written} records from {len(self.sources)} sources."
        broken = ", ".join(s.name for s in self.failures)
        return (
            f"Synced {self.written} records, but {broken} failed, "
            "so some of what I have is stale."
        )

    def detail(self) -> str:
        lines = ["| source | result | records |", "|---|---|---|"]
        for s in self.sources:
            mark = "ok" if s.ok else "FAILED"
            note = f" — {s.detail}" if s.detail else ""
            lines.append(f"| {s.name} | {mark}{note} | {s.written} |")
        return "\n".join(lines)


async def sync_courses_and_assignments(store: Store, config: Settings) -> list[SourceResult]:
    """Canvas. Read-only, and course-linked so assignments carry a period."""
    if not config.canvas_token or not config.canvas_base_url:
        return [
            SourceResult("canvas", True, 0, "not configured"),
        ]

    client = CanvasClient(config.canvas_base_url, config.canvas_token)

    try:
        courses = await client.courses()
    except SchoolError as exc:
        return [SourceResult("canvas courses", False, 0, str(exc))]

    linked = 0
    unmatched: list[str] = []
    for course in courses:
        try:
            existing = await store.course_id_for("canvas", course["external_id"])
            if existing:
                linked += 1
                continue
            # Match against what the seed describes. A refusal here is
            # deliberate: an unmatched course still works, just without a
            # period and teacher, whereas a wrong match would put someone
            # else's name on his homework.
            candidate = best_match(course["name"], await store.unlinked_courses())
            if candidate is not None:
                await store.link_course(
                    str(candidate["id"]), source="canvas",
                    external_id=course["external_id"],
                )
            else:
                unmatched.append(course["name"])
                await store.create_course(
                    name=course["name"], source="canvas",
                    external_id=course["external_id"],
                )
            linked += 1
        except Exception as exc:  # noqa: BLE001 - one bad course is not the sync
            log.warning("could not link course %s: %s", course["external_id"], exc)

    results = [
        SourceResult(
            "canvas courses", True, linked,
            f"{len(unmatched)} unmatched to a seeded period: {', '.join(unmatched[:3])}"
            if unmatched else "",
        )
    ]

    written = 0
    problems: list[str] = []
    for course in courses:
        try:
            items = await client.assignments(course["external_id"])
        except SchoolError as exc:
            problems.append(f"{course['name']}: {exc}")
            continue

        course_id = await store.course_id_for("canvas", course["external_id"])
        for item in items:
            try:
                await store.upsert_assignment(
                    title=item["title"],
                    source="canvas",
                    external_id=item["external_id"],
                    due_at=item["due_at"],
                    course_id=course_id,
                    status=item["status"],
                    points_possible=item["points_possible"],
                    points_earned=item["points_earned"],
                    url=item["url"],
                )
                written += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("could not store assignment %s: %s", item["external_id"], exc)

    results.append(
        SourceResult(
            "canvas assignments",
            not problems,
            written,
            "; ".join(problems[:2]) if problems else "",
        )
    )
    return results


async def sync_calendar(store: Store, config: Settings) -> SourceResult:
    if not config.calendar_ics_url:
        return SourceResult("calendar", True, 0, "not configured")

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=config.sync_past_days)
    end = now + timedelta(days=config.sync_future_days)

    try:
        body = await fetch_ics(config.calendar_ics_url)
        events = parse_ics(body, window_start=start, window_end=end, tz=config.timezone)
    except SchoolError as exc:
        return SourceResult("calendar", False, 0, str(exc))

    written = 0
    for event in events:
        try:
            await store.upsert_event(
                title=event["title"],
                starts_at=event["starts_at"],
                ends_at=event["ends_at"],
                all_day=event["all_day"],
                location=event["location"],
                source="ics",
                external_id=event["external_id"],
            )
            written += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("could not store event: %s", exc)

    try:
        await store.prune_events(before=start.date())
    except Exception as exc:  # noqa: BLE001 - pruning is housekeeping
        log.warning("event prune failed: %s", exc)

    return SourceResult("calendar", True, written)


async def generate_shifts(store: Store, config: Settings) -> SourceResult:
    """Work posts nothing, so the fixed 3-7 PM Mon-Fri rule is materialised."""
    # Local, not the container's clock -- see Agent.answer.
    today = datetime.now(ZoneInfo(config.timezone)).date()
    spans = planned_shifts(
        today,
        config.shift_weeks_ahead,
        tz=config.timezone,
        start_hour=config.shift_start_hour,
        end_hour=config.shift_end_hour,
    )
    written = 0
    for starts_at, ends_at in spans:
        try:
            await store.add_shift(starts_at, ends_at, kind="work", generated=True)
            written += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("could not store shift %s: %s", starts_at, exc)
    return SourceResult("shifts", True, written)


async def sync_all(store: Store, config: Settings) -> SyncReport:
    """Run every source. A failure in one never stops the rest."""
    report = SyncReport()

    canvas, calendar, shifts = await asyncio.gather(
        sync_courses_and_assignments(store, config),
        sync_calendar(store, config),
        generate_shifts(store, config),
        return_exceptions=True,
    )

    if isinstance(canvas, BaseException):
        report.sources.append(SourceResult("canvas", False, 0, str(canvas)))
    else:
        report.sources.extend(canvas)

    for name, result in (("calendar", calendar), ("shifts", shifts)):
        if isinstance(result, BaseException):
            report.sources.append(SourceResult(name, False, 0, str(result)))
        else:
            report.sources.append(result)

    status = "ok" if report.ok else "partial"
    try:
        await store.mark_job("entity_sync", status=status,
                             error="; ".join(s.detail for s in report.failures) or None)
    except Exception as exc:  # noqa: BLE001 - bookkeeping never fails a sync
        log.warning("could not record the sync job: %s", exc)

    return report
