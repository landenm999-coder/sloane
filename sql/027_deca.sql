-- The deca skill (sloane/skills/deca.py): practice role-plays, with the model
-- as the judge. One row per scored run, so she knows what he keeps missing.
-- Idempotent, like 001-026.
create table if not exists roleplays (
  id           uuid primary key default gen_random_uuid(),
  area         text        not null,
  event        text        not null,
  situation    text        not null,
  score        integer     not null,
  scores       jsonb       not null default '{}'::jsonb,
  strengths    text,
  improve      text,
  finished_at  timestamptz not null default now(),
  constraint roleplays_score_ok check (score between 0 and 100)
);

create index if not exists roleplays_finished_idx on roleplays (finished_at desc);
