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
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sloane.config import Settings
from sloane.memory.store import Store, remember
from sloane.school import SchoolError
from sloane.school.changes import classify
from sloane.school.calendar import fetch as fetch_ics, parse as parse_ics
from sloane.school.canvas import CanvasClient
from sloane.school.matching import best_match
from sloane.school.shifts import planned_shifts

log = logging.getLogger(__name__)

# Percentage points a course grade must move before it's worth a message.
GRADE_STEP = 2.0


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


async def _link_course(store: Store, external_id: str, name: str, unmatched: list[str]) -> None:
    """Give a Canvas course a row, matched to a seeded period where one fits."""
    if await store.course_id_for("canvas", external_id):
        return
    # Match against what the seed describes. A refusal here is deliberate: an
    # unmatched course still works, just without a period and teacher, whereas
    # a wrong match would put someone else's name on his homework.
    candidate = best_match(name, await store.unlinked_courses())
    if candidate is not None:
        await store.link_course(str(candidate["id"]), source="canvas", external_id=external_id)
    else:
        unmatched.append(name)
        await store.create_course(name=name, source="canvas", external_id=external_id)


async def sync_courses_and_assignments(store: Store, config: Settings) -> list[SourceResult]:
    """Canvas. Read-only, and course-linked so assignments carry a period."""
    if not config.canvas_token or not config.canvas_base_url:
        if config.canvas_feed_url:
            return await sync_canvas_feed(store, config)
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
            await _link_course(store, course["external_id"], course["name"], unmatched)
            linked += 1
        except Exception as exc:  # noqa: BLE001 - one bad course is not the sync
            log.warning("could not link course %s: %s", course["external_id"], exc)

    # Current grades, where Canvas shows them. A move of GRADE_STEP points or
    # more is news; the first grade ever seen is the baseline.
    for course in courses:
        try:
            cid = await store.course_id_for("canvas", course["external_id"])
            if cid is None:
                continue
            if course.get("score") is None:
                # Totals hidden now: clear the old grade rather than keep
                # presenting a stale number as current.
                await store.set_course_grade(cid, score=None, grade=None)
                continue
            previous = await store.set_course_grade(cid, score=course["score"], grade=course.get("grade"))
            if previous is not None and abs(course["score"] - previous) >= GRADE_STEP:
                letter = f" ({course['grade']})" if course.get("grade") else ""
                await remember("grade change", store.record_school_change(
                    assignment_id=None, kind="grade", title=course["name"], course=None,
                    detail=f"{previous:g}% → {course['score']:g}%{letter}",
                ))
        except Exception as exc:  # noqa: BLE001 - a grade is context, never the sync
            log.warning("could not store grade for %s: %s", course["external_id"], exc)

    results = [
        SourceResult(
            "canvas courses", True, linked,
            f"{len(unmatched)} unmatched to a seeded period: {', '.join(unmatched[:3])}"
            if unmatched else "",
        )
    ]

    written = 0
    problems: list[str] = []
    # The first sync ever is the baseline: everything is "new" to the database,
    # none of it is news to him.
    baseline = not await store.has_assignments("canvas")
    now = datetime.now(timezone.utc)
    for course in courses:
        try:
            items = await client.assignments(course["external_id"])
        except SchoolError as exc:
            problems.append(f"{course['name']}: {exc}")
            continue

        course_id = await store.course_id_for("canvas", course["external_id"])
        for item in items:
            try:
                before, after = await store.upsert_assignment(
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
                if not baseline:
                    for kind, detail in classify(before, after, now=now, tz=config.timezone):
                        await remember("school change", store.record_school_change(
                            assignment_id=str(after["id"]), kind=kind, title=item["title"],
                            course=course["name"], detail=detail,
                        ))
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


async def sync_canvas_feed(store: Store, config: Settings) -> list[SourceResult]:
    """Canvas without a token: its Calendar Feed. Due dates only; see canvas_feed.py."""
    from sloane.school.canvas_feed import parse_feed

    now = datetime.now(timezone.utc)
    try:
        body = await fetch_ics(config.canvas_feed_url, what="Canvas feed")
        items = parse_feed(body, window_start=now - timedelta(days=config.sync_past_days),
                           window_end=now + timedelta(days=config.sync_future_days), tz=config.timezone)
    except SchoolError as exc:
        return [SourceResult("canvas feed", False, 0, str(exc))]

    unmatched: list[str] = []
    for external_id, name in sorted({(i["course_external_id"], i["course_name"] or i["course_external_id"])
                                     for i in items if i["course_external_id"]}):
        try:
            await _link_course(store, external_id, name, unmatched)
        except Exception as exc:  # noqa: BLE001 - one bad course is not the sync
            log.warning("could not link course %s: %s", external_id, exc)

    written = 0
    baseline = not await store.has_assignments("canvas")
    for item in items:
        try:
            course_id = (await store.course_id_for("canvas", item["course_external_id"])
                         if item["course_external_id"] else None)
            before, after = await store.upsert_assignment(
                title=item["title"], source="canvas", external_id=item["external_id"],
                due_at=item["due_at"], course_id=course_id,
                # Ahead: open. Past: the feed can't say if it went in, so not overdue.
                status="open" if item["due_at"] > now else "unknown",
                url=item["url"],
            )
            written += 1
            if not baseline:
                for kind, detail in classify(before, after, now=now, tz=config.timezone):
                    await remember("school change", store.record_school_change(
                        assignment_id=str(after["id"]), kind=kind, title=item["title"],
                        course=item["course_name"], detail=detail,
                    ))
        except Exception as exc:  # noqa: BLE001
            log.warning("could not store assignment %s: %s", item["external_id"], exc)
    detail = "due dates only (no grades or turned-in state without a token)"
    if unmatched:
        detail += f"; {len(unmatched)} unmatched to a seeded period: {', '.join(unmatched[:3])}"
    return [SourceResult("canvas feed", True, written, detail)]


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

    # Only after a clean parse, and never on an empty one: a feed that parsed
    # to nothing is far more likely a Google hiccup than an empty semester, and
    # wiping the calendar on a hiccup is the worse mistake.
    if events and written == len(events):
        try:
            retired = await store.retire_events(
                source="ics", start=start, end=end, keep=[e["external_id"] for e in events]
            )
            if retired:
                log.info("retired %s calendar rows the feed no longer has", retired)
        except Exception as exc:  # noqa: BLE001 - housekeeping
            log.warning("event retire failed: %s", exc)

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
