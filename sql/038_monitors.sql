-- The monitors skill (sloane/skills/monitors.py): things he asked her to watch for and
-- tell him about once, when they happen. Idempotent, like 001-037. `said` is his own
-- words; `spec` is what she made of them (a ticker and a line, a page and a phrase, or
-- a question for the web); `last_value` is hers (a page's fingerprint, the last price).
create table if not exists monitors (
  id             uuid        primary key default gen_random_uuid(),
  said           text        not null,
  kind           text        not null,
  spec           jsonb       not null default '{}'::jsonb,
  every_minutes  integer     not null default 60,
  created_at     timestamptz not null default now(),
  checked_at     timestamptz,
  last_value     text,
  last_error     text,
  fired_at       timestamptz,
  fired_text     text,
  ended_at       timestamptz,
  ended_why      text,
  constraint monitors_said_ok check (length(said) between 1 and 300),
  constraint monitors_kind_ok check (kind in ('price', 'page', 'question')),
  constraint monitors_every_ok check (every_minutes between 5 and 1440)
);

create index if not exists monitors_open_idx on monitors (created_at) where ended_at is null;

-- The checks: every quarter hour in waking hours, off the heartbeat's minutes. Each monitor
-- has its own pace (a price every 15 minutes, a page hourly, a web question every few hours).
insert into jobs (name, cron) values ('monitors', '12,27,42,57 7-22 * * *')
on conflict (name) do nothing;
