-- Course grades from Canvas (read-only). Idempotent, like 001-009.
alter table courses add column if not exists current_score real;
alter table courses add column if not exists current_grade text;
alter table courses add column if not exists grade_updated_at timestamptz;

-- A course grade that moves is a change worth telling him about.
alter table school_changes drop constraint if exists school_changes_kind_ok;
alter table school_changes add constraint school_changes_kind_ok
  check (kind in ('new', 'graded', 'missing', 'due_moved', 'grade'));
