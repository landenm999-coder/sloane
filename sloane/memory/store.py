"""THE ONLY FILE THAT TALKS SQL.

Every statement in Sloane lives here or in a function that calls here. Agent,
telegram and job code ask this module questions; they never write a query. That
is what keeps tier 4 exact: there is one place to audit when a date looks wrong.

Two rules the callers depend on:

* Reads may raise. A caller that cannot get FACTS must say so, never guess.
* Writes may raise too, and the caller is expected to swallow it and still
  answer. A Supabase hiccup must not eat the morning brief. `remember()` is the
  wrapper that makes that the easy path.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Sequence
from datetime import date, datetime
from typing import Any, TypeVar

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from sloane.config import Settings, settings as default_settings

log = logging.getLogger(__name__)

T = TypeVar("T")

# Reciprocal Rank Fusion damping constant. 60 is the value from the paper
# that introduced RRF; it flattens the influence of the very top ranks so one
# arm cannot dominate the other.
RRF_K = 60

Row = dict[str, Any]


def as_vector(values: Sequence[float]) -> str:
    """Render an embedding the way pgvector's text input expects it.

    Done by hand so that pgvector-python is not a dependency: one fewer package
    that can break a deploy at 11 PM.
    """
    return "[" + ",".join(f"{float(v):.6g}" for v in values) + "]"


class Store:
    """An async handle on Postgres. One per process."""

    def __init__(self, config: Settings | None = None) -> None:
        self._config = config or default_settings()
        self._pool: AsyncConnectionPool | None = None

    # -- lifecycle ------------------------------------------------------------

    async def open(self) -> None:
        if self._pool is not None:
            return
        if not self._config.database_url:
            raise RuntimeError("DATABASE_URL is not set")
        self._pool = AsyncConnectionPool(
            self._config.database_url,
            min_size=1,
            max_size=4,  # one ARM core pair; a wide pool buys nothing
            open=False,
            # A pooler drops idle connections, and this process sits idle
            # between a 6:35 AM brief and the next message. Without a check,
            # the first query after a quiet stretch fails on a dead socket --
            # which would land as a missed morning brief, not a stack trace.
            check=AsyncConnectionPool.check_connection,
            max_idle=180.0,
            kwargs={
                "row_factory": dict_row,
                # Supabase's transaction-mode pooler (port 6543) does not
                # support prepared statements, and psycopg prepares
                # automatically after a few executions -- so the app works for
                # five queries and then fails. Session mode (5432) is fine, but
                # the failure is silent enough at setup time that it is not
                # worth leaving to whichever port got pasted. At a few queries
                # per turn the planning saved is unmeasurable.
                "prepare_threshold": None,
            },
        )
        await self._pool.open(wait=True, timeout=10)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def __aenter__(self) -> Store:
        await self.open()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    # -- plumbing -------------------------------------------------------------

    async def _fetch(self, sql: str, params: Sequence[Any] = ()) -> list[Row]:
        if self._pool is None:
            await self.open()
        assert self._pool is not None
        async with self._pool.connection() as conn:
            cur = await conn.execute(sql, params)
            return await cur.fetchall()

    async def _one(self, sql: str, params: Sequence[Any] = ()) -> Row | None:
        rows = await self._fetch(sql, params)
        return rows[0] if rows else None

    async def _exec(self, sql: str, params: Sequence[Any] = ()) -> None:
        if self._pool is None:
            await self.open()
        assert self._pool is not None
        async with self._pool.connection() as conn:
            await conn.execute(sql, params)

    async def healthy(self) -> bool:
        try:
            row = await self._one("select 1 as ok")
        except Exception:  # noqa: BLE001 - health must report, not raise
            return False
        return bool(row and row.get("ok") == 1)

    # -- tier 1: state --------------------------------------------------------

    async def get_state(self) -> list[Row]:
        """Every pinned durable fact. Rides in every prompt."""
        return await self._fetch(
            """
            select key, category, value, confidence
            from state
            where pinned
            order by category, key
            """
        )

    async def put_state(
        self,
        key: str,
        value: str,
        *,
        category: str = "fact",
        confidence: float = 1.0,
        source: str | None = None,
    ) -> None:
        await self._exec(
            """
            insert into state (key, value, category, confidence, source)
            values (%s, %s, %s, %s, %s)
            on conflict (key) do update
              set value = excluded.value,
                  category = excluded.category,
                  confidence = excluded.confidence,
                  source = excluded.source,
                  updated_at = now()
            """,
            (key, value, category, confidence, source),
        )

    # -- tier 2: working set --------------------------------------------------

    async def get_working_set(self, limit: int = 25) -> list[Row]:
        return await self._fetch(
            """
            select kind, summary, salience, opened_at
            from working_set
            where closed_at is null
            order by salience desc, opened_at desc
            limit %s
            """,
            (limit,),
        )

    async def add_working_item(
        self,
        summary: str,
        *,
        kind: str = "open_loop",
        salience: float = 0.5,
        ref_table: str | None = None,
        ref_id: str | None = None,
    ) -> None:
        await self._exec(
            """
            insert into working_set (kind, summary, salience, ref_table, ref_id)
            values (%s, %s, %s, %s, %s)
            """,
            (kind, summary, salience, ref_table, ref_id),
        )

    async def close_working_item(self, item_id: str) -> None:
        await self._exec(
            "update working_set set closed_at = now() where id = %s and closed_at is null",
            (item_id,),
        )

    async def rebuild_working_set(self, days: int = 7) -> int:
        """Close anything stale, then re-derive open loops from tier 4.

        P2 runs this nightly; it exists in P0 so tier 2 is real from the start.
        Returns the number of items now open.
        """
        await self._exec(
            """
            update working_set
               set closed_at = now()
             where closed_at is null
               and kind = 'recent'
               and opened_at < now() - make_interval(days => %s)
            """,
            (days,),
        )
        await self._exec(
            """
            insert into working_set (kind, summary, salience, ref_table, ref_id)
            select 'open_loop',
                   'Assignment due: ' || a.title,
                   0.8,
                   'assignments',
                   a.id
              from assignments a
             where a.status = 'open' and not a.done_locally
               and a.due_at is not null
               and a.due_at < now() + interval '7 days'
               and not exists (
                     select 1 from working_set w
                      where w.ref_table = 'assignments'
                        and w.ref_id = a.id
                        and w.closed_at is null
                   )
            """
        )
        # Close a loop unless its assignment still exists *and* is still open.
        # Phrased as NOT EXISTS on purpose: the obvious "EXISTS ... status <>
        # 'open'" never closes a loop whose assignment row is gone, and that
        # loop then haunts every brief forever.
        await self._exec(
            """
            update working_set w
               set closed_at = now()
             where w.closed_at is null
               and w.ref_table = 'assignments'
               and not exists (
                     select 1 from assignments a
                      where a.id = w.ref_id and a.status = 'open' and not a.done_locally
                   )
            """
        )
        # A loop's summary is written once; keep it in step with a renamed
        # assignment so a brief never quotes a title Canvas no longer uses.
        await self._exec(
            """
            update working_set w
               set summary = 'Assignment due: ' || a.title
              from assignments a
             where w.closed_at is null
               and w.ref_table = 'assignments'
               and w.ref_id = a.id
               and w.summary is distinct from 'Assignment due: ' || a.title
            """
        )
        row = await self._one(
            "select count(*) as n from working_set where closed_at is null"
        )
        return int(row["n"]) if row else 0

    # -- tier 3: episodes -----------------------------------------------------

    async def add_episode(
        self,
        content: str,
        *,
        role: str = "user",
        channel: str = "telegram",
        summary: str | None = None,
        embedding: Sequence[float] | None = None,
        tokens: int | None = None,
        occurred_at: datetime | None = None,
        trusted: bool = True,
        source: str | None = None,
    ) -> str:
        """Record one episode. `occurred_at` defaults to now.

        It is settable because ingested content carries its own timestamp: an
        email from Tuesday should decay from Tuesday, not from when we read it.

        `trusted` must be False for anything Landen did not write. The flag
        lives on the row, not on the turn, so the label survives into every
        later retrieval -- otherwise an injection read once becomes a standing
        instruction the first time recall surfaces it again.
        """
        row = await self._one(
            """
            insert into episodes
              (role, channel, content, summary, embedding, tokens, occurred_at,
               trusted, source)
            values (%s, %s, %s, %s, %s::vector, %s, coalesce(%s, now()), %s, %s)
            returning id
            """,
            (
                role,
                channel,
                content,
                summary,
                as_vector(embedding) if embedding is not None else None,
                tokens,
                occurred_at,
                trusted,
                source,
            ),
        )
        assert row is not None
        return str(row["id"])

    async def search_episodes(
        self,
        embedding: Sequence[float] | None = None,
        *,
        text: str = "",
        limit: int | None = None,
        half_life_days: float | None = None,
        include_untrusted: bool = False,
    ) -> list[Row]:
        """Hybrid recall: semantic and lexical arms, fused, then aged.

            score = RRF(vector_rank, fulltext_rank) * 0.5 ** (age_days / half_life)

        The two arms fail in opposite directions. Embeddings match paraphrase
        but blur rare proper nouns; full-text nails the exact token but misses
        a reworded question. Landen's history is dense with names that only
        ever appear one way -- DECA, Babcock, Jewelry I, Keegan -- so the
        lexical arm is doing real work here, not padding a benchmark.

        Fusion is Reciprocal Rank Fusion: each arm contributes 1/(k + rank).
        It needs no tuning and is immune to the two arms' incompatible score
        scales (cosine distance vs ts_rank_cd), which is why it beats trying to
        weight the raw scores against each other.

        The recency decay is applied to the fused score, so the documented
        14-day half-life still means what it meant: a hit twice that old is
        worth a quarter as much, whichever arm found it.

        Passing no embedding runs the lexical arm alone. That is the degraded
        path when the ONNX weights are missing, and it is why recall no longer
        dies with the embedder.
        """
        half_life = half_life_days or self._config.recency_half_life_days
        wanted = limit or self._config.retrieval_limit
        # Over-fetch per arm so fusion has something to reorder. Cheap at this
        # table size and the standard recommendation.
        candidates = max(wanted * 4, 50)

        params: dict[str, Any] = {
            "half_life": half_life,
            "limit": wanted,
            "candidates": candidates,
            "k": RRF_K,
            "include_untrusted": include_untrusted,
        }

        arms = []
        if embedding is not None:
            params["q"] = as_vector(embedding)
            arms.append(
                """
                vec as (
                  select id, row_number() over (order by embedding <=> %(q)s::vector) as rank
                    from episodes
                   where embedding is not null
                     and (%(include_untrusted)s or trusted)
                   order by embedding <=> %(q)s::vector
                   limit %(candidates)s
                )
                """
            )
        if text.strip():
            params["text"] = text
            arms.append(
                """
                qry as (
                  -- websearch_to_tsquery ANDs every term, so a natural question
                  -- ("what did Babcock say") would only match a row containing
                  -- all of its content words -- which a real one never does, and
                  -- the arm would silently contribute nothing. Rewriting & to |
                  -- gives the any-of retrieval this arm exists for, and leaves
                  -- ts_rank_cd to reward rows matching more and rarer terms.
                  -- Postgres still does the parsing, so nothing is interpolated.
                  select replace(
                           websearch_to_tsquery('english', %(text)s)::text,
                           '&', '|'
                         )::tsquery as tq
                ),
                fts as (
                  select e.id,
                         row_number() over (order by ts_rank_cd(e.tsv, qry.tq) desc) as rank
                    from episodes e, qry
                   where qry.tq is not null
                     and e.tsv @@ qry.tq
                     and (%(include_untrusted)s or e.trusted)
                   order by ts_rank_cd(e.tsv, qry.tq) desc
                   limit %(candidates)s
                                )
                """
            )

        if not arms:
            return []

        if len(arms) == 2:
            fused = """
                fused as (
                  select coalesce(v.id, f.id) as id,
                         (coalesce(1.0 / (%(k)s + v.rank), 0)
                           + coalesce(1.0 / (%(k)s + f.rank), 0))::float8 as rrf
                    from vec v
                    full outer join fts f on f.id = v.id
                )
            """
        else:
            only = "vec" if embedding is not None else "fts"
            fused = f"""
                fused as (
                  select id, (1.0 / (%(k)s + rank))::float8 as rrf from {only}
                )
            """

        sql = f"""
            with {",".join(arms)}, {fused}
            select e.id,
                   e.occurred_at,
                   e.role,
                   e.channel,
                   e.trusted,
                   e.source,
                   coalesce(e.summary, e.content) as text,
                   fused.rrf,
                   fused.rrf * power(
                       0.5,
                       (extract(epoch from (now() - e.occurred_at)) / 86400.0)
                         / %(half_life)s
                   ) as score
              from fused
              join episodes e on e.id = fused.id
             order by score desc
             limit %(limit)s
        """
        return await self._fetch(sql, params)

    async def recent_episodes(self, limit: int = 10) -> list[Row]:
        return await self._fetch(
            """
            select id, occurred_at, role, coalesce(summary, content) as text
              from episodes
             order by occurred_at desc
             limit %s
            """,
            (limit,),
        )

    # -- tier 4: entities. Exact or nothing. ---------------------------------

    async def assignments_due(
        self,
        start: date,
        end: date,
        *,
        include_done: bool = False,
    ) -> list[Row]:
        """Assignments due on a local-date range, inclusive at both ends.

        The cast to the configured timezone is the point: "due Friday" means
        Friday in Parker, not Friday in UTC.
        """
        return await self._fetch(
            """
            select a.id,
                   a.title,
                   a.due_at,
                   a.all_day,
                   a.status,
                   a.points_possible,
                   a.url,
                   c.name as course,
                   c.teacher,
                   c.period
              from assignments a
              left join courses c on c.id = a.course_id
             where a.due_at is not null
               and (a.due_at at time zone %(tz)s)::date between %(start)s and %(end)s
               and (%(include_done)s or (a.status = 'open' and not a.done_locally))
             order by a.due_at, c.period nulls last
            """,
            {
                "tz": self._config.timezone,
                "start": start,
                "end": end,
                "include_done": include_done,
            },
        )

    async def overdue_assignments(self) -> list[Row]:
        return await self._fetch(
            """
            select a.id, a.title, a.due_at, a.status, c.name as course
              from assignments a
              left join courses c on c.id = a.course_id
             where a.status in ('open', 'missing') and not a.done_locally
               and a.due_at is not null
               and a.due_at < now()
             order by a.due_at desc  -- most recent first: the ones still worth saving
            """
        )

    async def upsert_assignment(
        self,
        *,
        title: str,
        source: str,
        external_id: str | None,
        due_at: datetime | None = None,
        course_id: str | None = None,
        status: str = "open",
        all_day: bool = False,
        points_possible: float | None = None,
        points_earned: float | None = None,
        url: str | None = None,
    ) -> tuple[Row | None, Row | None]:
        """Idempotent on (source, external_id) so a re-sync updates, never doubles.

        Returns (before, after): the row as it was (None if new) and as it is,
        so the sync can notice what changed. Both None for an id-less insert.
        """
        before = None
        if external_id is not None:
            before = await self._one(
                """
                select id, status, due_at, points_earned from assignments
                 where source = %s and external_id = %s
                """,
                (source, external_id),
            )
        if external_id is None:
            await self._exec(
                """
                insert into assignments
                  (title, source, due_at, course_id, status, all_day,
                   points_possible, points_earned, url)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (title, source, due_at, course_id, status, all_day,
                 points_possible, points_earned, url),
            )
            return None, None
        after = await self._one(
            """
            insert into assignments
              (title, source, external_id, due_at, course_id, status, all_day,
               points_possible, points_earned, url)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (source, external_id) where external_id is not null
            do update set title = excluded.title,
                          due_at = excluded.due_at,
                          course_id = coalesce(excluded.course_id, assignments.course_id),
                          status = excluded.status,
                          all_day = excluded.all_day,
                          points_possible = excluded.points_possible,
                          points_earned = excluded.points_earned,
                          url = excluded.url,
                          updated_at = now()
            returning id, status, due_at, points_earned, points_possible, done_locally
            """,
            (title, source, external_id, due_at, course_id, status, all_day,
             points_possible, points_earned, url),
        )
        return before, after

    async def has_assignments(self, source: str) -> bool:
        row = await self._one("select exists(select 1 from assignments where source = %s) as any", (source,))
        return bool(row and row["any"])

    async def record_school_change(
        self, *, assignment_id: str | None, kind: str, title: str,
        course: str | None, detail: str | None,
    ) -> None:
        await self._exec(
            """
            insert into school_changes (assignment_id, kind, title, course, detail)
            values (%s, %s, %s, %s, %s)
            """,
            (assignment_id, kind, title, course, detail),
        )

    async def claim_school_changes(self, limit: int = 50) -> list[Row]:
        """Mark pending changes announced and return them, oldest first."""
        return await self._fetch(
            """
            update school_changes set notified_at = now()
             where id in (
               select id from school_changes where notified_at is null
                order by detected_at limit %s for update skip locked
             )
            returning *
            """,
            (limit,),
        )

    async def unclaim_school_changes(self, ids: Sequence[str]) -> None:
        await self._exec(
            "update school_changes set notified_at = null where id = any(%s::uuid[])", (list(ids),)
        )

    async def outstanding_assignments(self) -> list[Row]:
        """Open or missing, not marked done by him: what /done can match against."""
        return await self._fetch(
            """
            select a.id, a.title, a.due_at, a.status, c.name as course
              from assignments a
              left join courses c on c.id = a.course_id
             where a.status in ('open', 'missing') and not a.done_locally
             order by a.due_at nulls last
            """
        )

    async def mark_done_locally(self, assignment_id: str, done: bool = True) -> Row | None:
        return await self._one(
            "update assignments set done_locally = %s where id = %s returning id, title",
            (done, assignment_id),
        )

    async def courses(self, semester: str | None = None) -> list[Row]:
        return await self._fetch(
            """
            select id, period, name, teacher, semester, room, source, external_id,
                   current_score, current_grade, grade_updated_at
              from courses
             where active
               and (%s::text is null or semester = %s::text)
             order by period nulls last
            """,
            (semester, semester),
        )

    async def upsert_course(
        self,
        *,
        name: str,
        period: int | None = None,
        teacher: str | None = None,
        semester: str = "S1",
        room: str | None = None,
        source: str = "manual",
        external_id: str | None = None,
    ) -> None:
        if period is not None:
            await self._exec(
                """
                insert into courses (name, period, teacher, semester, room, source, external_id)
                values (%s, %s, %s, %s, %s, %s, %s)
                on conflict (semester, period) where period is not null
                do update set name = excluded.name,
                              teacher = excluded.teacher,
                              room = excluded.room,
                              source = excluded.source,
                              external_id = coalesce(excluded.external_id, courses.external_id)
                """,
                (name, period, teacher, semester, room, source, external_id),
            )
            return
        await self._exec(
            """
            insert into courses (name, period, teacher, semester, room, source, external_id)
            values (%s, null, %s, %s, %s, %s, %s)
            on conflict (source, external_id) where external_id is not null
            do update set name = excluded.name, teacher = excluded.teacher
            """,
            (name, teacher, semester, room, source, external_id),
        )

    async def shifts_between(self, start: date, end: date) -> list[Row]:
        return await self._fetch(
            """
            select id, starts_at, ends_at, kind, cancelled, note
              from shifts
             where not cancelled
               and (starts_at at time zone %(tz)s)::date between %(start)s and %(end)s
             order by starts_at
            """,
            {"tz": self._config.timezone, "start": start, "end": end},
        )

    async def add_shift(
        self,
        starts_at: datetime,
        ends_at: datetime,
        *,
        kind: str = "work",
        generated: bool = True,
        note: str | None = None,
    ) -> None:
        """Idempotent on (kind, starts_at): re-running the generator is safe."""
        await self._exec(
            """
            insert into shifts (starts_at, ends_at, kind, generated, note)
            values (%s, %s, %s, %s, %s)
            on conflict (kind, starts_at) do nothing
            """,
            (starts_at, ends_at, kind, generated, note),
        )

    async def unlinked_courses(self) -> list[Row]:
        """Seeded courses no upstream id has claimed yet."""
        return await self._fetch(
            """
            select id, name, period, teacher, semester
              from courses
             where active and external_id is null
             order by period nulls last
            """
        )

    async def link_course(self, course_id: str, *, source: str, external_id: str) -> None:
        """Attach an upstream id to a course the seed already described."""
        await self._exec(
            "update courses set source = %s, external_id = %s where id = %s",
            (source, external_id, course_id),
        )

    async def set_course_grade(self, course_id: str, *, score: float | None,
                               grade: str | None) -> float | None:
        """Store the current grade; returns the score it replaced (None if none)."""
        row = await self._one(
            """
            with before as (select current_score from courses where id = %s)
            update courses
               set current_score = %s, current_grade = %s, grade_updated_at = now()
             where id = %s
            returning (select current_score from before) as previous
            """,
            (course_id, score, grade, course_id),
        )
        return row["previous"] if row else None

    async def create_course(self, *, name: str, source: str, external_id: str) -> str:
        """Record an upstream course the seed does not describe.

        It gets no period and no teacher, which is honest: nothing here knows
        them. Assignments still attach and still answer by name.
        """
        row = await self._one(
            """
            insert into courses (name, source, external_id)
            values (%s, %s, %s)
            on conflict (source, external_id) where external_id is not null
            do update set name = excluded.name
            returning id
            """,
            (name, source, external_id),
        )
        assert row is not None
        return str(row["id"])

    async def course_id_for(self, source: str, external_id: str) -> str | None:
        row = await self._one(
            "select id from courses where source = %s and external_id = %s",
            (source, external_id),
        )
        return str(row["id"]) if row else None

    # -- calendar events ------------------------------------------------------

    async def upsert_event(
        self,
        *,
        title: str,
        starts_at: datetime,
        ends_at: datetime | None = None,
        all_day: bool = False,
        location: str | None = None,
        source: str = "ics",
        external_id: str | None = None,
    ) -> None:
        """Idempotent on (source, external_id) so a re-sync never duplicates."""
        await self._exec(
            """
            insert into events
              (title, starts_at, ends_at, all_day, location, source, external_id)
            values (%s, %s, %s, %s, %s, %s, %s)
            on conflict (source, external_id) where external_id is not null
            do update set title = excluded.title,
                          starts_at = excluded.starts_at,
                          ends_at = excluded.ends_at,
                          all_day = excluded.all_day,
                          location = excluded.location,
                          updated_at = now()
            """,
            (title, starts_at, ends_at, all_day, location, source, external_id),
        )

    async def events_between(self, start: date, end: date) -> list[Row]:
        return await self._fetch(
            """
            select id, title, starts_at, ends_at, all_day, location, trusted
              from events
             -- Overlap, not start: on the Wednesday of a Monday-to-Friday
             -- break, the break is still on. Local days, exclusive end.
             where (starts_at at time zone %(tz)s) < (%(end)s::date + 1)::timestamp
               and ((coalesce(ends_at, starts_at) at time zone %(tz)s) > %(start)s::date::timestamp
                    or (starts_at at time zone %(tz)s) >= %(start)s::date::timestamp)
             order by starts_at
            """,
            {"tz": self._config.timezone, "start": start, "end": end},
        )

    async def retire_events(
        self, *, source: str, start: datetime, end: datetime, keep: Sequence[str]
    ) -> int:
        """Delete this source's rows in [start, end] that the feed no longer has.

        An event moved from Thursday to Friday has a new external id, and one
        deleted upstream has none; without this both linger as phantom FACTS
        (and phantom conflicts) until they drift out of the window.
        """
        row = await self._one(
            """
            with gone as (
              delete from events
               where source = %s
                 and starts_at between %s and %s
                 and not (external_id = any(%s))
              returning 1
            )
            select count(*) as n from gone
            """,
            (source, start, end, list(keep)),
        )
        return int(row["n"]) if row else 0

    async def prune_events(self, before: date) -> int:
        """Drop occurrences that have fallen out of the sync window.

        Not a hard-line violation: these are regenerated copies of an upstream
        feed, not anything Landen wrote. Without it, a weekly event accumulates
        a row a week forever.
        """
        row = await self._one(
            """
            with gone as (
              delete from events
               where source = 'ics'
                 and (starts_at at time zone %(tz)s)::date < %(before)s
              returning 1
            )
            select count(*) as n from gone
            """,
            {"tz": self._config.timezone, "before": before},
        )
        return int(row["n"]) if row else 0

    async def people(self, limit: int = 50) -> list[Row]:
        return await self._fetch(
            """
            select id, name, relation, handle, email, notes
              from people
             order by relation, name
             limit %s
            """,
            (limit,),
        )

    async def upsert_person(
        self,
        name: str,
        *,
        relation: str = "unknown",
        handle: str | None = None,
        email: str | None = None,
        notes: str | None = None,
    ) -> None:
        await self._exec(
            """
            insert into people (name, relation, handle, email, notes)
            values (%s, %s, %s, %s, %s)
            on conflict (lower(name), relation)
            do update set handle = coalesce(excluded.handle, people.handle),
                          email = coalesce(excluded.email, people.email),
                          notes = coalesce(excluded.notes, people.notes)
            """,
            (name, relation, handle, email, notes),
        )

    async def open_commitments(self) -> list[Row]:
        return await self._fetch(
            """
            select c.id, c.what, c.due_at, c.promised_at, p.name as person
              from commitments c
              left join people p on p.id = c.person_id
             where c.status = 'open'
             order by c.due_at nulls last, c.promised_at
            """
        )

    async def add_commitment(
        self,
        what: str,
        *,
        person_id: str | None = None,
        due_at: datetime | None = None,
        source_episode_id: str | None = None,
    ) -> Row:
        row = await self._one(
            """
            insert into commitments (what, person_id, due_at, source_episode_id)
            values (%s, %s, %s, %s)
            returning *
            """,
            (what, person_id, due_at, source_episode_id),
        )
        assert row is not None
        return row

    async def person_id(self, name: str) -> str:
        """The id for this name, creating the person if new. Case-insensitive."""
        row = await self._one(
            """
            with found as (
              select id from people where lower(name) = lower(%s) order by created_at limit 1
            ), made as (
              insert into people (name, relation)
              select %s, 'unknown' where not exists (select 1 from found)
              on conflict (lower(name), relation) do update set name = people.name
              returning id
            )
            select id from found union all select id from made limit 1
            """,
            (name, name),
        )
        assert row is not None
        return str(row["id"])

    async def close_commitment(self, commitment_id: str, status: str = "kept") -> Row | None:
        return await self._one(
            """
            update commitments set status = %s, closed_at = now()
             where id = %s and status = 'open'
             returning *
            """,
            (status, commitment_id),
        )

    async def reminders_between(self, start: datetime, end: datetime) -> list[Row]:
        return await self._fetch(
            """
            select id, text, due_at from reminders
             where sent_at is null and cancelled_at is null and due_at between %s and %s
             order by due_at
            """,
            (start, end),
        )

    # -- trust ----------------------------------------------------------------

    async def hard_lines(self) -> list[Row]:
        """The six rows. A record of the gate, read by the code that is the gate."""
        return await self._fetch(
            "select action, target, reason from trust where hard_line order by action"
        )

    async def trust_for(self, action: str, target: str) -> Row | None:
        return await self._one(
            """
            select action, target, state, clean_streak, reversals, hard_line, decays_at
              from trust
             where action = %s and target = %s
            """,
            (action, target),
        )

    async def trust_ledger(self) -> list[Row]:
        return await self._fetch(
            """
            select action, target, state, clean_streak, reversals, hard_line,
                   unlocked_at, decays_at
              from trust
             order by hard_line desc, state desc, action, target
            """
        )

    async def is_trusted(self, action: str, target: str, now: datetime) -> bool:
        """Trusted means unlocked, not yet decayed, and never a hard line.

        The hard-line clause is belt and braces: ensure_allowed() has already
        refused those pairs in code before this is ever asked.
        """
        row = await self._one(
            """
            select 1 as ok from trust
             where action = %s and target = %s
               and state = 'trusted' and not hard_line
               and decays_at > %s
            """,
            (action, target, now),
        )
        return row is not None

    async def record_trust(
        self,
        action: str,
        target: str,
        outcome: str,
        *,
        now: datetime,
        unlock_after: int,
        decay_days: int,
    ) -> Row | None:
        """Move the ledger for one exact pair. Hard-line rows are never touched.

        outcome is one of:
          clean     a straight approval -- the streak grows, and at
                    `unlock_after` the pair becomes trusted for `decay_days`
          edited    he had to change it -- the streak resets, still gated
          reversal  a deny or a revoke -- straight back to gated
          used      a trusted pair ran again -- the decay window restarts
          expired   the decay window lapsed unused -- gated, streak reset
        """
        if outcome not in {"clean", "edited", "reversal", "used", "expired"}:
            raise ValueError(f"unknown trust outcome {outcome!r}")

        # Make sure the pair has a row. Exact pairs only: "reply to Keegan"
        # earns nothing for "reply to anyone".
        await self._exec(
            """
            insert into trust (action, target) values (%s, %s)
            on conflict (action, target) do nothing
            """,
            (action, target),
        )
        return await self._one(
            """
            update trust t set
              clean_streak = case %(o)s
                when 'clean' then t.clean_streak + 1
                when 'used' then t.clean_streak
                else 0 end,
              reversals = t.reversals + (case when %(o)s = 'reversal' then 1 else 0 end),
              state = case
                when %(o)s in ('edited', 'reversal', 'expired') then 'gated'
                when %(o)s = 'clean' and t.clean_streak + 1 >= %(n)s then 'trusted'
                else t.state end,
              unlocked_at = case
                when %(o)s = 'clean' and t.state = 'gated'
                     and t.clean_streak + 1 >= %(n)s then %(now)s
                when %(o)s in ('edited', 'reversal', 'expired') then null
                else t.unlocked_at end,
              decays_at = case
                when %(o)s in ('edited', 'reversal', 'expired') then null
                when %(o)s = 'used' and t.state = 'trusted'
                     then %(now)s + make_interval(days => %(d)s)
                when %(o)s = 'clean' and t.clean_streak + 1 >= %(n)s
                     then %(now)s + make_interval(days => %(d)s)
                else t.decays_at end,
              last_change_at = %(now)s
             where t.action = %(a)s and t.target = %(t)s and not t.hard_line
             returning action, target, state, clean_streak, reversals, decays_at
            """,
            {"o": outcome, "n": unlock_after, "d": decay_days, "now": now,
             "a": action, "t": target},
        )

    async def lapsed_trust(self, now: datetime) -> list[Row]:
        return await self._fetch(
            """
            select action, target from trust
             where state = 'trusted' and not hard_line
               and (decays_at is null or decays_at <= %s)
            """,
            (now,),
        )

    # -- proposals --------------------------------------------------------------

    async def create_proposal(
        self,
        *,
        action: str,
        target: str,
        preview: str,
        payload: dict,
        status: str = "pending",
        auto: bool = False,
        result: str | None = None,
    ) -> Row:
        row = await self._one(
            """
            insert into proposals (action, target, preview, payload, status, auto, result)
            values (%s, %s, %s, %s, %s, %s, %s)
            returning *
            """,
            (action, target, preview, Jsonb(payload), status, auto, result),
        )
        assert row is not None
        return row

    async def get_proposal(self, proposal_id: str) -> Row | None:
        return await self._one("select * from proposals where id = %s", (proposal_id,))

    async def set_proposal_message(self, proposal_id: str, message_id: int) -> None:
        await self._exec(
            "update proposals set message_id = %s where id = %s", (message_id, proposal_id)
        )

    async def transition_proposal(
        self, proposal_id: str, *, from_status: str, to_status: str
    ) -> Row | None:
        """Atomic: moves only if the row is still in `from_status`.

        This is what makes a double-tapped button, or two racing callbacks,
        execute once. The loser gets None.
        """
        return await self._one(
            """
            update proposals
               set status = %s,
                   decided_at = coalesce(decided_at, now()),
                   edited = edited or %s = 'editing'
             where id = %s and status = %s
             returning *
            """,
            (to_status, to_status, proposal_id, from_status),
        )

    async def revise_proposal(self, proposal_id: str, *, preview: str, payload: dict) -> Row | None:
        """Replace an editing proposal's content and put it back up for approval."""
        return await self._one(
            """
            update proposals
               set preview = %s, payload = %s, status = 'pending', decided_at = null
             where id = %s and status = 'editing'
             returning *
            """,
            (preview, Jsonb(payload), proposal_id),
        )

    async def finish_proposal(self, proposal_id: str, *, ok: bool, result: str) -> None:
        await self._exec(
            """
            update proposals
               set status = %s, result = %s, executed_at = now()
             where id = %s
            """,
            ("executed" if ok else "failed", result[:2000], proposal_id),
        )

    async def editing_proposal(self) -> Row | None:
        return await self._one(
            """
            select * from proposals where status = 'editing'
             order by decided_at desc nulls last limit 1
            """
        )

    async def editing_proposals(self) -> list[Row]:
        return await self._fetch(
            "select * from proposals where status = 'editing' order by decided_at"
        )

    async def open_proposals(self) -> list[Row]:
        return await self._fetch(
            """
            select * from proposals where status in ('pending', 'editing')
             order by created_at
            """
        )

    # -- reminders ----------------------------------------------------------------

    async def add_reminder(self, *, text: str, due_at: datetime, source: str = "telegram") -> Row:
        row = await self._one(
            "insert into reminders (text, due_at, source) values (%s, %s, %s) returning *",
            (text, due_at, source),
        )
        assert row is not None
        return row

    async def claim_due_reminders(self, now: datetime, limit: int = 20) -> list[Row]:
        """Mark due reminders sent and return them, atomically: each is claimed once."""
        return await self._fetch(
            """
            update reminders set sent_at = %s
             where id in (
               select id from reminders
                where due_at <= %s and sent_at is null and cancelled_at is null
                order by due_at
                limit %s
                for update skip locked
             )
            returning *
            """,
            (now, now, limit),
        )

    async def claim_snooze(self, reminder_id: str) -> Row | None:
        """Mark a reminder snoozed, once. None if it already was (or is gone)."""
        return await self._one(
            """
            update reminders set snoozed_at = now()
             where id = %s and snoozed_at is null
             returning *
            """,
            (reminder_id,),
        )

    async def get_reminder(self, reminder_id: str) -> Row | None:
        return await self._one("select * from reminders where id = %s", (reminder_id,))

    async def unclaim_reminder(self, reminder_id: str) -> None:
        await self._exec("update reminders set sent_at = null where id = %s", (reminder_id,))

    async def upcoming_reminders(self, limit: int = 20) -> list[Row]:
        return await self._fetch(
            """
            select * from reminders
             where sent_at is null and cancelled_at is null
             order by due_at
             limit %s
            """,
            (limit,),
        )

    async def cancel_reminder(self, reminder_id: str) -> Row | None:
        return await self._one(
            """
            update reminders set cancelled_at = now()
             where id = %s and sent_at is null and cancelled_at is null
             returning *
            """,
            (reminder_id,),
        )

    # -- watchdog -------------------------------------------------------------------

    async def open_alerts(self) -> list[Row]:
        return await self._fetch("select * from alerts where resolved_at is null order by first_seen")

    async def see_alert(self, key: str, message: str, now: datetime) -> Row:
        """Record that a problem is present. A resolved or new key starts fresh."""
        row = await self._one(
            """
            insert into alerts (key, message, first_seen) values (%s, %s, %s)
            on conflict (key) do update
               set message = excluded.message,
                   first_seen = case when alerts.resolved_at is null
                                     then alerts.first_seen else excluded.first_seen end,
                   notified_at = case when alerts.resolved_at is null
                                      then alerts.notified_at else null end,
                   resolved_at = null
            returning *
            """,
            (key, message, now),
        )
        assert row is not None
        return row

    async def mark_alert_notified(self, key: str, now: datetime) -> None:
        await self._exec("update alerts set notified_at = %s where key = %s", (now, key))

    async def resolve_alert(self, key: str, now: datetime) -> None:
        await self._exec("update alerts set resolved_at = %s where key = %s", (now, key))

    async def provider_health(self, hours: int) -> list[Row]:
        """Per provider over the window: attempts and failures."""
        return await self._fetch(
            """
            select provider, count(*) as calls, count(*) filter (where not ok) as failures,
                   max(error) filter (where not ok) as sample_error
              from usage_log
             where at > now() - make_interval(hours => %s)
             group by provider
            """,
            (hours,),
        )

    # -- weekly review & backup ---------------------------------------------------

    async def school_changes_since(self, since: datetime) -> list[Row]:
        return await self._fetch(
            "select kind, title, course, detail, detected_at from school_changes "
            "where detected_at >= %s order by detected_at",
            (since,),
        )

    async def commitments_closed_since(self, since: datetime) -> list[Row]:
        return await self._fetch(
            "select what, status, closed_at from commitments where closed_at >= %s order by closed_at",
            (since,),
        )

    # Only what cannot be rebuilt from upstream. Canvas, the calendar and shifts
    # re-sync; these were typed, promised, earned or decided by Landen.
    BACKUP_TABLES = ("state", "commitments", "people", "courses", "trust", "reminders", "jobs")

    async def restore_rows(self, table: str, rows: Sequence[Row]) -> int:
        """Merge backed-up rows back in. Existing rows win; returns rows inserted.

        Merge, never overwrite: a restore brings back what was lost without
        undoing anything changed since the backup. Column names come from the
        file, so only columns the table really has are used.
        """
        if table not in self.BACKUP_TABLES:
            raise ValueError(f"{table} is not a backed-up table")
        if not rows:
            return 0
        real = {
            r["column_name"] for r in await self._fetch(
                "select column_name from information_schema.columns "
                "where table_schema = 'public' and table_name = %s",
                (table,),
            )
        }
        inserted = 0
        for row in rows:
            cols = [c for c in row if c in real]
            if not cols:
                continue
            names = ", ".join(f'"{c}"' for c in cols)
            marks = ", ".join(["%s"] * len(cols))
            # Identifiers are checked against information_schema above.
            done = await self._one(
                f"insert into {table} ({names}) values ({marks}) "  # noqa: S608
                "on conflict do nothing returning 1 as ok",
                [row[c] for c in cols],
            )
            inserted += 1 if done else 0
        return inserted

    async def export(self) -> dict[str, list[Row]]:
        out: dict[str, list[Row]] = {}
        for table in self.BACKUP_TABLES:
            # Table names come from the constant above, never from input.
            out[table] = await self._fetch(f"select * from {table}")  # noqa: S608
        return out

    # -- email (P4) -------------------------------------------------------------

    async def known_emails(self, gmail_ids: Sequence[str]) -> set[str]:
        """Which of these have already been triaged. Each message is judged once."""
        if not gmail_ids:
            return set()
        rows = await self._fetch(
            "select gmail_id from emails where gmail_id = any(%s)", (list(gmail_ids),)
        )
        return {r["gmail_id"] for r in rows}

    async def record_email(
        self,
        *,
        gmail_id: str,
        thread_id: str,
        sender: str,
        sender_name: str,
        subject: str,
        snippet: str,
        received_at: datetime | None,
        category: str,
        why: str,
    ) -> bool:
        """True if this call recorded it; False if another run got there first."""
        row = await self._one(
            """
            insert into emails
              (gmail_id, thread_id, sender, sender_name, subject, snippet,
               received_at, category, why)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (gmail_id) do nothing
            returning id
            """,
            (gmail_id, thread_id, sender, sender_name, subject, snippet,
             received_at, category, why),
        )
        return row is not None

    async def link_email_proposal(self, gmail_id: str, proposal_id: str) -> None:
        await self._exec(
            "update emails set proposal_id = %s where gmail_id = %s", (proposal_id, gmail_id)
        )

    async def recent_emails(self, limit: int = 20) -> list[Row]:
        return await self._fetch(
            """
            select e.gmail_id, e.sender, e.sender_name, e.subject, e.category,
                   e.why, e.received_at, e.triaged_at, p.status as proposal_status
              from emails e
              left join proposals p on p.id = e.proposal_id
             order by e.triaged_at desc
             limit %s
            """,
            (limit,),
        )

    # -- ops ------------------------------------------------------------------

    async def log_usage(
        self,
        *,
        provider: str,
        model: str | None = None,
        purpose: str = "reply",
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        latency_ms: int | None = None,
        ok: bool = True,
        error: str | None = None,
        degraded_from: str | None = None,
    ) -> None:
        await self._exec(
            """
            insert into usage_log
              (provider, model, purpose, prompt_tokens, completion_tokens,
               latency_ms, ok, error, degraded_from)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (provider, model, purpose, prompt_tokens, completion_tokens,
             latency_ms, ok, error, degraded_from),
        )

    async def usage_summary(self, hours: int = 24) -> list[Row]:
        return await self._fetch(
            """
            select provider,
                   purpose,
                   count(*) as calls,
                   count(*) filter (where not ok) as failures,
                   count(*) filter (where degraded_from is not null) as degraded,
                   coalesce(sum(prompt_tokens), 0) as prompt_tokens,
                   coalesce(sum(completion_tokens), 0) as completion_tokens,
                   round(avg(latency_ms)) as avg_latency_ms
              from usage_log
             where at > now() - make_interval(hours => %s)
             group by provider, purpose
             order by calls desc
            """,
            (hours,),
        )

    async def usage_today(self) -> dict[str, int]:
        """Calls made so far today, by purpose, counted in local days.

        Local, not UTC: the free tiers Landen is riding reset on their own
        clocks, but what he cares about is "have I burned today", and today
        ends at midnight in Parker.
        """
        rows = await self._fetch(
            """
            select purpose, count(*) as n
              from usage_log
             where (at at time zone %(tz)s)::date = (now() at time zone %(tz)s)::date
             group by purpose
            """,
            {"tz": self._config.timezone},
        )
        return {r["purpose"]: int(r["n"]) for r in rows}

    async def next_update_offset(self) -> int:
        """The Telegram long-poll cursor, derived rather than stored separately.

        Telegram only drops an update once a higher offset is acknowledged, so
        max(update_id) + 1 means a restart neither replays nor loses one.
        """
        row = await self._one("select coalesce(max(update_id), 0) as high from messages")
        return int(row["high"]) + 1 if row else 1

    async def log_message(
        self,
        *,
        update_id: int | None = None,
        chat_id: int | None = None,
        direction: str = "in",
        kind: str = "text",
        body: str | None = None,
        file_id: str | None = None,
        episode_id: str | None = None,
    ) -> bool:
        """Record one transport event. False if this update_id was already seen."""
        row = await self._one(
            """
            insert into messages
              (update_id, chat_id, direction, kind, body, file_id, episode_id)
            values (%s, %s, %s, %s, %s, %s, %s)
            on conflict (update_id) do nothing
            returning id
            """,
            (update_id, chat_id, direction, kind, body, file_id, episode_id),
        )
        return row is not None

    async def jobs(self) -> list[Row]:
        return await self._fetch(
            """
            select name, cron, enabled, runs, last_run_at, last_status, last_error
              from jobs
             order by name
            """
        )

    async def mark_job(
        self, name: str, *, status: str, error: str | None = None
    ) -> None:
        await self._exec(
            """
            update jobs
               set last_run_at = now(),
                   last_status = %s,
                   last_error = %s,
                   runs = runs + 1
             where name = %s
            """,
            (status, error, name),
        )

    # -- skills: sessions (sql/013) ---------------------------------------------
    #
    # Each skill's queries get their own section below this one, headed with the
    # skill and its migration, like this one.

    async def active_session(self, idle_minutes: int) -> Row | None:
        """The open skill session. One left idle too long is closed first."""
        await self._exec(
            """
            update skill_sessions set ended_at = now()
             where ended_at is null and touched_at < now() - make_interval(mins => %s)
            """,
            (idle_minutes,),
        )
        return await self._one("select * from skill_sessions where ended_at is null")

    async def start_session(self, skill: str, state: dict) -> Row:
        """Open a session, ending whichever one was open."""
        await self._exec("update skill_sessions set ended_at = now() where ended_at is null")
        row = await self._one(
            "insert into skill_sessions (skill, state) values (%s, %s) returning *",
            (skill, Jsonb(state)),
        )
        assert row is not None
        return row

    async def touch_session(self, session_id: str, state: dict) -> None:
        await self._exec(
            """
            update skill_sessions set state = %s, touched_at = now()
             where id = %s and ended_at is null
            """,
            (Jsonb(state), session_id),
        )

    async def end_session(self, session_id: str | None = None) -> Row | None:
        """End one session, or whichever is open. None if nothing was open."""
        if session_id is None:
            return await self._one(
                "update skill_sessions set ended_at = now() where ended_at is null returning *"
            )
        return await self._one(
            "update skill_sessions set ended_at = now() where id = %s and ended_at is null returning *",
            (session_id,),
        )


async def remember(what: str, coro: Awaitable[T]) -> T | None:
    """Run a memory write that must never cost us the reply.

    Invariant 5: wrap persistence and still answer. Every write path in agent
    and telegram code goes through here, so a Supabase hiccup shows up in the
    log rather than as a missing morning brief.
    """
    try:
        return await coro
    except Exception:  # noqa: BLE001 - deliberate: the reply outranks the write
        log.exception("memory write failed and was dropped: %s", what)
        return None
