-- The countdowns skill (sloane/skills/countdowns.py): graduation, prom, DECA
-- districts -- a named day and how far off it is. Idempotent, like 001-015.
-- Dropping one archives it; nothing is deleted.
create table if not exists countdowns (
  id           uuid primary key default gen_random_uuid(),
  name         text        not null,
  on_date      date        not null,
  created_at   timestamptz not null default now(),
  archived_at  timestamptz
);

create index if not exists countdowns_upcoming_idx
  on countdowns (on_date) where archived_at is null;
