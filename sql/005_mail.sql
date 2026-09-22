-- P4: Gmail triage. Idempotent, like 001-004.
--
-- One row per message she has triaged, so a message is classified once no
-- matter how often the inbox job runs, and a draft is proposed at most once.
-- Only metadata and a short snippet are kept here; the body she triaged from is
-- stored as an untrusted episode, since it was written by whoever sent it.

create table if not exists emails (
  id            uuid primary key default gen_random_uuid(),
  gmail_id      text        not null unique,
  thread_id     text,
  sender        text,
  sender_name   text,
  subject       text,
  snippet       text,
  received_at   timestamptz,
  category      text,
  why           text,
  proposal_id   uuid references proposals (id) on delete set null,
  triaged_at    timestamptz not null default now(),
  constraint emails_category_ok check (
    category is null or category in ('urgent', 'reply', 'fyi', 'ignore')
  )
);

create index if not exists emails_triaged_idx on emails (triaged_at desc);

insert into jobs (name, cron) values ('inbox', '10 7-21/3 * * *')
on conflict (name) do nothing;
