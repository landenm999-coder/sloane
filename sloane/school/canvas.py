"""Canvas, read-only.

Only GET. There is no code path here that submits work, comments, or writes
anything back -- school systems are read-only to Sloane, and that is a property
of this file, not a rule in a prompt.

Canvas paginates with RFC 5988 Link headers rather than a cursor in the body,
and the `next` link is the only reliable way forward: page counts are missing
on several collections.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

import httpx

from sloane.ingest import safe_field
from sloane.school import SchoolError

log = logging.getLogger(__name__)

_NEXT_LINK = re.compile(r'<([^>]+)>\s*;\s*rel="next"')

# Canvas pages at 10 by default, which turns one course's assignments into
# several round trips for no reason.
PER_PAGE = 100

# Enough pages to cover a school year; a runaway `next` chain stops here rather
# than looping against the district's servers.
MAX_PAGES = 20


def _next_url(link_header: str | None) -> str | None:
    if not link_header:
        return None
    found = _NEXT_LINK.search(link_header)
    return found.group(1) if found else None


def _parse_time(value: Any) -> datetime | None:
    """Canvas emits ISO 8601 with a trailing Z."""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        log.warning("unparseable canvas timestamp: %r", value)
        return None


def _status(assignment: dict) -> str:
    """Map Canvas's submission state onto ours.

    Canvas reports several shapes here depending on the endpoint and the
    include[] flags, so this reads defensively and falls back to 'open' --
    the status that keeps a thing visible. Guessing 'submitted' would hide
    real work.
    """
    submission = assignment.get("submission")
    if not isinstance(submission, dict):
        return "open"
    if submission.get("missing"):
        return "missing"
    if submission.get("excused"):
        return "excused"
    state = submission.get("workflow_state")
    if state == "graded":
        return "graded"
    if state in {"submitted", "pending_review"}:
        return "submitted"
    if submission.get("submitted_at"):
        return "submitted"
    return "open"


class CanvasClient:
    def __init__(self, base_url: str, token: str, *, timeout: float = 30.0) -> None:
        self._base = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout

    async def _get_all(self, path: str, **params: Any) -> list[dict]:
        """Follow `next` links until the collection is exhausted."""
        if not self._token:
            raise SchoolError("CANVAS_TOKEN is not set")
        if not self._base:
            raise SchoolError("CANVAS_BASE_URL is not set")

        url = f"{self._base}{path}"
        query: dict[str, Any] | None = {"per_page": PER_PAGE, **params}
        out: list[dict] = []

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            for _ in range(MAX_PAGES):
                try:
                    response = await client.get(
                        url,
                        params=query,
                        headers={"Authorization": f"Bearer {self._token}"},
                    )
                except httpx.HTTPError as exc:
                    raise SchoolError(f"canvas unreachable: {exc}") from exc

                if response.status_code == 401:
                    raise SchoolError("canvas rejected the token (401)")
                if response.status_code == 403:
                    raise SchoolError("canvas refused the request (403)")
                if response.status_code >= 400:
                    raise SchoolError(
                        f"canvas {path} -> {response.status_code}: {response.text[:200]}"
                    )

                try:
                    page = response.json()
                except ValueError as exc:
                    raise SchoolError(f"canvas returned non-JSON: {exc}") from exc
                if not isinstance(page, list):
                    raise SchoolError(f"canvas {path} did not return a list")

                out.extend(p for p in page if isinstance(p, dict))

                url = _next_url(response.headers.get("link"))
                if not url:
                    return out
                # The next link already carries the query string.
                query = None

        log.warning("stopped following canvas pagination at %s pages", MAX_PAGES)
        return out

    async def courses(self) -> list[dict]:
        """Active courses, normalised and sanitised."""
        raw = await self._get_all(
            "/api/v1/courses",
            enrollment_state="active",
            state=["available"],
        )
        courses = []
        for c in raw:
            if c.get("access_restricted_by_date"):
                continue
            name = safe_field(c.get("name"), limit=120)
            if not name or c.get("id") is None:
                continue
            courses.append({"external_id": str(c["id"]), "name": name})
        return courses

    async def assignments(self, course_id: str) -> list[dict]:
        """Assignments for one course, with submission state folded in."""
        raw = await self._get_all(
            f"/api/v1/courses/{course_id}/assignments",
            **{"include[]": "submission", "order_by": "due_at"},
        )
        out = []
        for a in raw:
            if a.get("id") is None:
                continue
            title = safe_field(a.get("name"), limit=200)
            if not title:
                continue
            out.append(
                {
                    "external_id": str(a["id"]),
                    "title": title,
                    "due_at": _parse_time(a.get("due_at")),
                    "status": _status(a),
                    "points_possible": a.get("points_possible"),
                    "points_earned": (a.get("submission") or {}).get("score")
                    if isinstance(a.get("submission"), dict)
                    else None,
                    "url": safe_field(a.get("html_url"), limit=400) or None,
                    "course_external_id": str(course_id),
                }
            )
        return out
