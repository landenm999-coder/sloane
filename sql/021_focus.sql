-- The focus skill (sloane/skills/focus.py): timed focus sessions. Idempotent,
-- like 001-020. The "time's up" message is an ordinary reminder row, so it is
-- delivered, held through quiet hours and retried like any other.
create table if not exists focus_sessions (
  id           uuid primary key default gen_random_uuid(),
  what         text        not null,
  minutes      integer     not null,
  started_at   timestamptz not null default now(),
  ends_at      timestamptz not null,
  stopped_at   timestamptz,
  -- The reminder that says time's up. Not a foreign key: a skill never
  -- constrains a core table, and cancelling a reminder that is gone is a no-op.
  reminder_id  uuid,
  constraint focus_minutes_ok check (minutes between 1 and 240)
);

create index if not exists focus_sessions_started_idx on focus_sessions (started_at desc);
