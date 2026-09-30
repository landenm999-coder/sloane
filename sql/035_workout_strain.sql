-- The workouts skill (sloane/skills/workouts.py), continued: Whoop's strain and average heart
-- rate for the workouts it records. Idempotent, like 001-034. Null for the ones he logs himself.
alter table workouts add column if not exists strain numeric;
alter table workouts add column if not exists heart_rate integer;

do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'workouts_strain_ok') then
    alter table workouts add constraint workouts_strain_ok check (strain is null or strain between 0 and 21);
  end if;
  if not exists (select 1 from pg_constraint where conname = 'workouts_heart_rate_ok') then
    alter table workouts add constraint workouts_heart_rate_ok check (heart_rate is null or heart_rate between 20 and 250);
  end if;
end $$;
