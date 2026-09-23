-- Skills (sloane/skills/). A skill may run an ongoing session -- a quiz, a
-- practice round -- that claims Landen's next plain messages until it ends or
-- goes stale. One open session at a time, enforced here rather than trusted.
create table if not exists skill_sessions (
  id          uuid primary key default gen_random_uuid(),
  skill       text not null,
  state       jsonb not null default '{}'::jsonb,
  started_at  timestamptz not null default now(),
  touched_at  timestamptz not null default now(),
  ended_at    timestamptz
);

create unique index if not exists skill_sessions_one_open
  on skill_sessions ((true)) where ended_at is null;
