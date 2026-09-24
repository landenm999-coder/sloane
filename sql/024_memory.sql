-- What she learns from the day's conversation (sloane/memory/learn.py) and the
-- memory skill that shows it. Idempotent, like 001-023.
--
-- A follow-up is a working_set row of kind 'follow_up' ("call the dentist"),
-- with the day it's for when he said one. Learned facts are state rows of
-- category 'learned', which /forget unpins rather than deletes.
alter table working_set add column if not exists due_on date;

-- 12:20 AM, after the 12:15 reflection. Silent: it never messages him.
insert into jobs (name, cron) values ('learn', '20 0 * * *')
on conflict (name) do nothing;
