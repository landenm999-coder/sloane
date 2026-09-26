-- The heartbeat: every quarter hour in waking hours, ask each skill whether
-- anything is worth saying now, and say each thing once. Idempotent, like 001-013.
--
-- A skill offers the same nudge (same key) every tick until its condition
-- clears; this table is what makes it said once. `offered_at` is refreshed on
-- every tick that still offers the key, so a row is pruned only after the skill
-- has stopped offering it for a month -- never while it would be repeated.

create table if not exists nudges_said (
  key         text primary key,
  said_at     timestamptz not null default now(),
  offered_at  timestamptz not null default now()
);

-- :05, :20, :35, :50 past each hour from 7 AM to 10 PM. Off the quarter hours
-- so it never lands in the same minute as a brief. No model call, ever.
insert into jobs (name, cron) values ('heartbeat', '5,20,35,50 7-22 * * *')
on conflict (name) do nothing;
