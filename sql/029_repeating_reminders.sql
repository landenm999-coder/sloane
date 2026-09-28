-- Repeating reminders. Idempotent, like 001-028.
--
-- `repeat` is the rule ("days:0,1,2,3,4", "every:2", "hours:3", "month:1",
-- see sloane/reminders.py). Each occurrence is its own row; delivering one
-- inserts the next, with the same series_id. The unique index makes that
-- insert safe to retry: a second attempt for the same time is a no-op.
-- Cancelling the pending row ends the series.
alter table reminders add column if not exists repeat text;
alter table reminders add column if not exists series_id uuid;

create unique index if not exists reminders_series_due_idx on reminders (series_id, due_at)
  where series_id is not null;
