-- The workshop (sloane/workshop.py): features she builds on herself, with his
-- say at every step. Idempotent, like 001-030.
--
-- One row per idea, his or hers, from the idea to live (or not):
--
--   idea -> planned -> queued -> building -> ready -> deploying -> live
--                                        \-> failed      \-> denied / rolled_back / undone
--
-- `ready` is built on its own branch, every test passed, CI green: waiting on
-- his Accept or Deny in the dashboard. Nothing reaches the running code any
-- other way.
create table if not exists workshop_items (
  id          uuid primary key default gen_random_uuid(),
  title       text not null,
  request     text not null,
  -- him: he asked. her: her own idea (nightly), built for him to judge.
  origin      text not null default 'him' check (origin in ('him', 'her')),
  kind        text not null default 'build' check (kind in ('build', 'undo')),
  undoes      uuid references workshop_items(id) on delete set null,
  status      text not null default 'idea',
  plan        text,
  -- What she says she built, the files it touched, the checks it passed.
  summary     text,
  files       text[] not null default '{}',
  checks      text,
  branch      text,
  pr_number   integer,
  pr_url      text,
  commit_sha  text,
  merge_sha   text,
  error       text,
  -- Why he said no: she reads these before she thinks of her next idea.
  feedback    text,
  attempts    integer not null default 0,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),
  built_at    timestamptz,
  decided_at  timestamptz,
  live_at     timestamptz
);

create index if not exists workshop_items_status_idx on workshop_items (status, created_at);

-- 1:10 AM: his queued builds first, then (with room left) one of her own.
-- Quiet hours are for messages, not work; she tells him in the morning.
insert into jobs (name, cron) values ('workshop', '10 1 * * *')
on conflict (name) do nothing;
