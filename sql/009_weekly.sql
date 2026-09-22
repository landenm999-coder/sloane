-- The Sunday review and the nightly backup. Idempotent, like 001-008.
insert into jobs (name, cron) values
  ('weekly_review', '0 19 * * 0'),   -- Sunday 7 PM: the week behind, the week ahead
  ('backup',        '30 0 * * *')    -- 12:30 AM: silent JSON export of hand-made data
on conflict (name) do nothing;
