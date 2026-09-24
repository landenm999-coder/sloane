-- The colleges skill (sloane/skills/colleges.py): his applications, each
-- with a plan (EA, ED, RD ...), a deadline and a checklist. Idempotent, like
-- 001-025. Tracking only: nothing here submits an application or writes to a
-- school. Dropping one archives it; nothing is deleted.
create table if not exists colleges (
  id            uuid primary key default gen_random_uuid(),
  name          text        not null,
  nickname      text,
  plan          text,
  deadline      date,
  status        text        not null default 'applying',
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),
  submitted_on  date,
  archived_at   timestamptz,
  constraint colleges_plan_ok
    check (plan is null or plan in ('ED', 'ED2', 'EA', 'REA', 'RD', 'rolling', 'priority')),
  constraint colleges_status_ok
    check (status in ('applying', 'submitted', 'admitted', 'deferred', 'waitlisted', 'denied', 'committed'))
);

create unique index if not exists colleges_name_key
  on colleges (lower(name)) where archived_at is null;

create table if not exists college_tasks (
  id          bigserial primary key,
  college_id  uuid        not null references colleges (id) on delete cascade,
  task        text        not null,
  position    integer     not null default 0,
  due_on      date,
  state       text        not null default 'open',
  created_at  timestamptz not null default now(),
  closed_at   timestamptz,
  constraint college_tasks_state_ok check (state in ('open', 'done', 'skipped'))
);

create index if not exists college_tasks_college_idx on college_tasks (college_id, position, id);
