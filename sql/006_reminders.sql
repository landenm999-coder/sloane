-- Timed reminders. Idempotent, like 001-005.
--
-- A reminder is claimed (sent_at set) in the same statement that selects it,
-- so two ticks racing -- or a restart mid-send -- deliver it once. A send that
-- fails is unclaimed and retried on the next tick.

create table if not exists reminders (
  id            uuid primary key default gen_random_uuid(),
  text          text        not null,
  due_at        timestamptz not null,
  source        text        not null default 'telegram',
  created_at    timestamptz not null default now(),
  sent_at       timestamptz,
  cancelled_at  timestamptz
);

create index if not exists reminders_due_idx on reminders (due_at)
  where sent_at is null and cancelled_at is null;

-- Every minute. The job does no model call and sends nothing unless one is due.
insert into jobs (name, cron) values ('reminders', '* * * * *')
on conflict (name) do nothing;
