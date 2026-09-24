-- The birthdays skill (sloane/skills/birthdays.py): a month and day on the
-- people he knows. Idempotent, like 001-021. No year: the day is what matters,
-- and an age is a guess nobody asked for.
alter table people add column if not exists birth_month smallint;
alter table people add column if not exists birth_day smallint;

do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'people_birthday_ok') then
    alter table people add constraint people_birthday_ok check (
      (birth_month is null and birth_day is null)
      or (birth_month between 1 and 12 and birth_day between 1 and 31)
    );
  end if;
end $$;
