-- The watchdog's memory of what is wrong. Idempotent, like 001-007.
--
-- One row per distinct problem key ("job:morning_brief", "provider:claude_code").
-- A problem is told once it has lasted a grace period, repeated daily while it
-- lasts, and followed by a "fixed" message when it clears.

create table if not exists alerts (
  key          text primary key,
  message      text        not null,
  first_seen   timestamptz not null default now(),
  notified_at  timestamptz,
  resolved_at  timestamptz
);

insert into jobs (name, cron) values ('watchdog', '*/30 * * * *')
on conflict (name) do nothing;
