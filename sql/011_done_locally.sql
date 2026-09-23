-- "/done lab writeup": he handed it in, Canvas hasn't caught up. Idempotent.
-- The sync never writes this column, so a later Canvas pull can't undo it.
alter table assignments add column if not exists done_locally boolean not null default false;
