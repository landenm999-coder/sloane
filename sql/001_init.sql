-- Sloane P0 schema. Idempotent: safe to re-run against a live database.
--
-- Twelve tables across the four memory tiers plus the operational log.
--   tier 1  state         durable facts, always in the prompt
--   tier 2  working_set   last 7 days and open loops, rebuilt nightly
--   tier 3  episodes      everything, embedded, cosine x recency decay
--   tier 4  courses assignments shifts people commitments  -- SQL, exact or nothing
--   ops     trust usage_log jobs messages
--
-- pgvector note: plain vector(384), never halfvec. halfvec needs pgvector 0.7+
-- and Supabase does not publish its version. At ~2 KB a row that is still
-- more than ten years of history inside the 500 MB free tier.

create extension if not exists vector;

-- ---------------------------------------------------------------- tier 1 ----

create table if not exists state (
  id          uuid primary key default gen_random_uuid(),
  key         text        not null unique,
  category    text        not null default 'fact',
  value       text        not null,
  confidence  real        not null default 1.0,
  source      text,
  pinned      boolean     not null default true,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

create index if not exists state_category_idx on state (category) where pinned;

-- ---------------------------------------------------------------- tier 2 ----

create table if not exists working_set (
  id          uuid primary key default gen_random_uuid(),
  kind        text        not null default 'open_loop',
  summary     text        not null,
  ref_table   text,
  ref_id      uuid,
  salience    real        not null default 0.5,
  opened_at   timestamptz not null default now(),
  closed_at   timestamptz,
  rebuilt_at  timestamptz not null default now()
);

create index if not exists working_set_open_idx
  on working_set (salience desc, opened_at desc) where closed_at is null;

-- ---------------------------------------------------------------- tier 3 ----

create table if not exists episodes (
  id           uuid primary key default gen_random_uuid(),
  occurred_at  timestamptz not null default now(),
  role         text        not null default 'user',
  channel      text        not null default 'telegram',
  content      text        not null,
  summary      text,
  embedding    vector(384),
  tokens       integer,
  created_at   timestamptz not null default now()
);

create index if not exists episodes_occurred_idx on episodes (occurred_at desc);

-- HNSW, not IVFFlat: it needs no training pass, so it stays correct while the
-- table is still small and grows with it.
create index if not exists episodes_embedding_idx
  on episodes using hnsw (embedding vector_cosine_ops);

-- ---------------------------------------------------------------- tier 4 ----
-- Exact or nothing. A similarity search must never answer "what is due Friday".

create table if not exists courses (
  id           uuid primary key default gen_random_uuid(),
  period       integer,
  name         text        not null,
  teacher      text,
  semester     text        not null default 'S1',
  room         text,
  source       text        not null default 'manual',
  external_id  text,
  active       boolean     not null default true,
  created_at   timestamptz not null default now()
);

create unique index if not exists courses_period_key
  on courses (semester, period) where period is not null;
create unique index if not exists courses_external_key
  on courses (source, external_id) where external_id is not null;

create table if not exists assignments (
  id              uuid primary key default gen_random_uuid(),
  course_id       uuid references courses (id) on delete set null,
  title           text        not null,
  due_at          timestamptz,
  all_day         boolean     not null default false,
  status          text        not null default 'open',
  points_possible real,
  points_earned   real,
  source          text        not null default 'canvas',
  external_id     text,
  url             text,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);

create unique index if not exists assignments_external_key
  on assignments (source, external_id) where external_id is not null;
create index if not exists assignments_due_idx
  on assignments (due_at) where status = 'open';

-- Work never posts a schedule. Shifts are a fixed 3-7 PM Mon-Fri rule that the
-- generator materialises, not something to scrape.
create table if not exists shifts (
  id         uuid primary key default gen_random_uuid(),
  starts_at  timestamptz not null,
  ends_at    timestamptz not null,
  kind       text        not null default 'work',
  generated  boolean     not null default true,
  cancelled  boolean     not null default false,
  note       text,
  created_at timestamptz not null default now(),
  constraint shifts_span_ok check (ends_at > starts_at)
);

create unique index if not exists shifts_slot_key on shifts (kind, starts_at);

create table if not exists people (
  id              uuid primary key default gen_random_uuid(),
  name            text        not null,
  relation        text        not null default 'unknown',
  handle          text,
  email           text,
  notes           text,
  last_contact_at timestamptz,
  created_at      timestamptz not null default now()
);

create unique index if not exists people_name_key on people (lower(name), relation);

create table if not exists commitments (
  id                uuid primary key default gen_random_uuid(),
  what              text        not null,
  person_id         uuid references people (id) on delete set null,
  due_at            timestamptz,
  status            text        not null default 'open',
  promised_at       timestamptz not null default now(),
  closed_at         timestamptz,
  source_episode_id uuid references episodes (id) on delete set null
);

create index if not exists commitments_open_idx
  on commitments (due_at nulls last) where status = 'open';

-- ------------------------------------------------------------------- ops ----

-- A record of the gate, never the gate itself. The six hard_line rows are
-- enforced as pre-execution checks in code; a rule that lives only in a prompt
-- is a suggestion.
create table if not exists trust (
  id             uuid primary key default gen_random_uuid(),
  action         text        not null,
  target         text        not null,
  state          text        not null default 'gated',
  clean_streak   integer     not null default 0,
  reversals      integer     not null default 0,
  hard_line      boolean     not null default false,
  reason         text,
  unlocked_at    timestamptz,
  decays_at      timestamptz,
  last_change_at timestamptz not null default now(),
  constraint trust_state_ok check (state in ('gated', 'trusted'))
);

create unique index if not exists trust_pair_key on trust (action, target);

create table if not exists usage_log (
  id                bigserial primary key,
  at                timestamptz not null default now(),
  provider          text        not null,
  model             text,
  purpose           text        not null default 'reply',
  prompt_tokens     integer,
  completion_tokens integer,
  latency_ms        integer,
  ok                boolean     not null default true,
  error             text,
  degraded_from     text
);

create index if not exists usage_log_at_idx on usage_log (at desc);

create table if not exists jobs (
  id          uuid primary key default gen_random_uuid(),
  name        text        not null unique,
  cron        text        not null,
  enabled     boolean     not null default true,
  runs        integer     not null default 0,
  last_run_at timestamptz,
  last_status text,
  last_error  text,
  next_run_at timestamptz
);

-- Transport log. Doubles as the Telegram long-poll cursor: the next offset is
-- max(update_id) + 1, so a restart never replays or drops an update.
create table if not exists messages (
  id         uuid primary key default gen_random_uuid(),
  update_id  bigint unique,
  chat_id    bigint,
  direction  text        not null default 'in',
  kind       text        not null default 'text',
  body       text,
  file_id    text,
  episode_id uuid references episodes (id) on delete set null,
  at         timestamptz not null default now()
);

create index if not exists messages_at_idx on messages (at desc);

-- ------------------------------------------------------------- seed rows ----

insert into trust (action, target, hard_line, reason) values
  ('submit',  'schoolwork',    true, 'She never submits work. School systems are read-only.'),
  ('place',   'trade',         true, 'No order ever reaches a broker without Landen placing it.'),
  ('move',    'money',         true, 'No transfer, purchase or payment, in any amount.'),
  ('delete',  'anything',      true, 'Nothing irreversible. Archive instead.'),
  ('contact', 'school_staff',  true, 'No unprompted mail to teachers, counsellors or admin.'),
  ('publish', 'public',        true, 'She never posts publicly as Landen.')
on conflict (action, target) do update
  set hard_line = true, reason = excluded.reason;

insert into jobs (name, cron) values
  ('morning_brief',  '35 6 * * *'),
  ('pre_shift',      '45 14 * * 1-5'),
  ('post_shift',     '5 19 * * 1-5'),
  ('wrap',           '0 22 * * *'),
  ('reflection',     '15 0 * * *')
on conflict (name) do nothing;
