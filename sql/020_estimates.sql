-- The plan skill (sloane/skills/plan.py): how long he says an assignment will
-- take. Idempotent, like 001-019. Like done_locally, the sync never writes
-- this column, so a Canvas pull can't undo his estimate.
alter table assignments add column if not exists estimate_minutes integer;

do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'assignments_estimate_ok') then
    alter table assignments add constraint assignments_estimate_ok
      check (estimate_minutes is null or estimate_minutes between 1 and 1440);
  end if;
end $$;
