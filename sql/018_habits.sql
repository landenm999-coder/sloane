-- The habits skill (sloane/skills/habits.py): things he means to do daily,
-- and the streak. Idempotent, like 001-017. Dropping a habit archives it.
create table if not exists habits (
  id           uuid primary key default gen_random_uuid(),
  name         text        not null,
  created_at   timestamptz not null default now(),
  archived_at  timestamptz
);

create unique index if not exists habits_name_key
  on habits (lower(name)) where archived_at is null;

-- One row per habit per local day it was done. The primary key makes logging
-- the same day twice a no-op.
create table if not exists habit_log (
  habit_id   uuid        not null references habits (id) on delete cascade,
  on_date    date        not null,
  logged_at  timestamptz not null default now(),
  primary key (habit_id, on_date)
);
