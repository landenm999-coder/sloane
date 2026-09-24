-- Capture retries safely. The client sends a client_id per capture; a retry
-- after a lost response finds it here and gets the first answer back instead
-- of a second episode and a second reminder. Idempotent, like 001-027.
-- Refs older than 30 days are pruned as new ones arrive.
create table if not exists capture_refs (
  client_id  text        primary key,
  body       jsonb,
  at         timestamptz not null default now()
);

create index if not exists capture_refs_at_idx on capture_refs (at);
