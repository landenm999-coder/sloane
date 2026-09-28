-- She thinks (jobs/briefs.py `think`): a few times a day she looks over all she
-- knows and speaks only when one thing is worth raising. Idempotent, like 001-029.
--
-- 10:25, 12:25, 16:25 and 20:25. On a weekday the job itself sits out school
-- hours and any shift in progress, so that is the evening; a weekend gets all
-- four.
insert into jobs (name, cron) values ('think', '25 10,12,16,20 * * *')
on conflict (name) do nothing;
