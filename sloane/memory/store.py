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
from collections.abc import Awaitable, Callable, Sequence
from datetime import date, datetime
from typing import Any, TypeVar

from psycopg.rows import dict_row
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
             where a.status = 'open'
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
                      where a.id = w.ref_id and a.status = 'open'
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
               and (%(include_done)s or a.status = 'open')
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
             where a.status in ('open', 'missing')
               and a.due_at is not null
               and a.due_at < now()
             order by a.due_at
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
    ) -> None:
        """Idempotent on (source, external_id) so a re-sync updates, never doubles."""
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
            return
        await self._exec(
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
            """,
            (title, source, external_id, due_at, course_id, status, all_day,
             points_possible, points_earned, url),
        )

    async def courses(self, semester: str | None = None) -> list[Row]:
        return await self._fetch(
            """
            select id, period, name, teacher, semester, room, source, external_id
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
             where (starts_at at time zone %(tz)s)::date between %(start)s and %(end)s
             order by starts_at
            """,
            {"tz": self._config.timezone, "start": start, "end": end},
        )

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
    ) -> None:
        await self._exec(
            """
            insert into commitments (what, person_id, due_at, source_episode_id)
            values (%s, %s, %s, %s)
            """,
            (what, person_id, due_at, source_episode_id),
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
