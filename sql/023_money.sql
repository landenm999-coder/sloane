-- The money skill (sloane/skills/money.py): what he spends, against a weekly
-- budget. Idempotent, like 001-022. Tracking only -- moving money is a hard
-- line, and nothing here can. "Undo" archives; nothing is deleted.
create table if not exists expenses (
  id           uuid primary key default gen_random_uuid(),
  cents        integer     not null,
  what         text        not null,
  category     text        not null default 'other',
  spent_on     date        not null,
  created_at   timestamptz not null default now(),
  archived_at  timestamptz,
  constraint expenses_cents_ok check (cents > 0 and cents < 10000000)
);

create index if not exists expenses_spent_idx on expenses (spent_on) where archived_at is null;

-- Small per-skill settings (a weekly budget, a preference). One row per key.
create table if not exists skill_settings (
  skill       text        not null,
  key         text        not null,
  value       text        not null,
  updated_at  timestamptz not null default now(),
  primary key (skill, key)
);
