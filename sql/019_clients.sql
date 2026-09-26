-- The clients skill (sloane/skills/clients.py): the pipeline for his AI
-- website business. Idempotent, like 001-018. Tracking only: nothing here
-- sends, invoices or moves money (that is a hard line).
create table if not exists clients (
  id                uuid primary key default gen_random_uuid(),
  name              text        not null,
  stage             text        not null default 'lead',
  value_cents       integer,
  follow_up_on      date,
  next_step         text,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now(),
  stage_changed_at  timestamptz not null default now(),
  archived_at       timestamptz,
  constraint clients_stage_ok
    check (stage in ('lead', 'talking', 'proposal', 'building', 'live', 'paid', 'lost')),
  constraint clients_value_ok check (value_cents is null or value_cents >= 0)
);

create unique index if not exists clients_name_key
  on clients (lower(name)) where archived_at is null;

create table if not exists client_notes (
  id         bigserial primary key,
  client_id  uuid        not null references clients (id) on delete cascade,
  at         timestamptz not null default now(),
  note       text        not null
);

create index if not exists client_notes_client_idx on client_notes (client_id, at desc);
