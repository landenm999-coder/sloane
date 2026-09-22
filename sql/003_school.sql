-- P1: calendar events. Idempotent, like 001 and 002.
--
-- Assignments, shifts, courses and people already have homes. A calendar entry
-- is none of those: it is a block of time Landen did not promise anyone and
-- that work did not generate. It gets its own table rather than being bent into
-- `shifts` (which would make the name a lie) or `commitments` (which means
-- something he owes a person).
--
-- Recurring events are stored expanded, one row per occurrence, keyed by
-- uid:occurrence so a re-sync updates rather than duplicates.

create table if not exists events (
  id          uuid primary key default gen_random_uuid(),
  starts_at   timestamptz not null,
  ends_at     timestamptz,
  all_day     boolean     not null default false,
  title       text        not null,
  location    text,
  source      text        not null default 'ics',
  external_id text,
  -- Calendar text is written by whoever made the invite, not by Landen.
  trusted     boolean     not null default false,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),
  constraint events_span_ok check (ends_at is null or ends_at >= starts_at)
);

create unique index if not exists events_external_key
  on events (source, external_id) where external_id is not null;
create index if not exists events_starts_idx on events (starts_at);

comment on column events.trusted is
  'False by default: an invite title comes from whoever created it. Rendered '
  'as a fact about the calendar, never as something Landen said.';

-- The sync job P1 adds, alongside the five rhythm jobs seeded in 001.
insert into jobs (name, cron) values ('entity_sync', '0 */4 * * *')
on conflict (name) do nothing;
