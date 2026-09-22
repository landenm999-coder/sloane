-- P4: proposals and the approval flow. Idempotent, like 001-003.
--
-- Anything Sloane wants to *do* -- send an email, set a reminder -- becomes a
-- proposal. A proposal either executes because Landen has trusted that exact
-- (action, target) pair, or waits for Approve / Edit / Deny on Telegram.
--
-- The trust ledger itself is the `trust` table from 001. The six hard-line
-- rows there are a record; the gate is ensure_allowed() in agent.py, which runs
-- before a proposal is even stored.

create table if not exists proposals (
  id           uuid primary key default gen_random_uuid(),
  action       text        not null,
  target       text        not null,
  preview      text        not null,
  payload      jsonb       not null default '{}'::jsonb,
  status       text        not null default 'pending',
  -- Set once Landen presses Edit. An approval of an edited proposal is not a
  -- clean approval: the P4 gate is ten sent *without* edits.
  edited       boolean     not null default false,
  auto         boolean     not null default false,
  result       text,
  message_id   bigint,
  created_at   timestamptz not null default now(),
  decided_at   timestamptz,
  executed_at  timestamptz,
  constraint proposals_status_ok check (
    status in ('pending', 'editing', 'approved', 'denied', 'executed',
               'failed', 'refused')
  )
);

create index if not exists proposals_open_idx
  on proposals (created_at desc) where status in ('pending', 'editing');
