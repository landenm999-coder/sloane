-- A delivered reminder can be snoozed once. A double tap on a snooze button
-- arrives as two callbacks; claiming this column makes the second a no-op.
alter table reminders add column if not exists snoozed_at timestamptz;
