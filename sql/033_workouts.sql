-- The workouts skill (sloane/skills/workouts.py): what he did, how long, how far.
-- Idempotent, like 001-032. Logged from his words ("ran 3 miles", "45 minutes of
-- lifting"), or brought in from Whoop (sloane/skills/whoop.py), whose id keeps a
-- second sync from logging the same workout twice.
create table if not exists workouts (
  id          uuid primary key default gen_random_uuid(),
  kind        text        not null,
  minutes     integer,
  distance_m  numeric,
  done_on     date        not null,
  logged_at   timestamptz not null default now(),
  source      text        not null default 'him',
  source_id   text,
  constraint workouts_kind_ok check (length(kind) between 1 and 60),
  constraint workouts_minutes_ok check (minutes is null or minutes between 1 and 1440),
  constraint workouts_distance_ok check (distance_m is null or distance_m between 0 and 500000),
  constraint workouts_source_ok check (source in ('him', 'whoop'))
);

create index if not exists workouts_done_idx on workouts (done_on desc);
create unique index if not exists workouts_source_once on workouts (source, source_id) where source_id is not null;
