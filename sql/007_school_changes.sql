-- Canvas change alerts. Idempotent, like 001-006.
--
-- One row per change noticed during a sync (a new assignment, a grade, a
-- missing mark, a moved due date). Rows are announced once, outside quiet
-- hours, and kept for a while as a record of what she told him.

create table if not exists school_changes (
  id             uuid primary key default gen_random_uuid(),
  assignment_id  uuid references assignments (id) on delete cascade,
  kind           text        not null,
  title          text        not null,
  course         text,
  detail         text,
  detected_at    timestamptz not null default now(),
  notified_at    timestamptz,
  constraint school_changes_kind_ok check (kind in ('new', 'graded', 'missing', 'due_moved'))
);

create index if not exists school_changes_pending_idx on school_changes (detected_at)
  where notified_at is null;
