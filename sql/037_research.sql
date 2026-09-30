-- The research skill (sloane/skills/research.py): the questions he asked her to look
-- into properly, and her reports. Idempotent, like 001-036. A report is built from
-- strangers' web pages: outside text, stored untrusted and never in FACTS. The
-- question is his own words.
create table if not exists research_reports (
  id           uuid        primary key default gen_random_uuid(),
  question     text        not null,
  status       text        not null default 'running',
  report       text,
  error        text,
  trusted      boolean     not null default false,
  started_at   timestamptz not null default now(),
  finished_at  timestamptz,
  constraint research_question_ok check (length(question) between 1 and 500),
  constraint research_status_ok check (status in ('running', 'done', 'failed'))
);

create index if not exists research_reports_started_idx on research_reports (started_at desc);
