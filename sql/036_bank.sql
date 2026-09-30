-- The bank skill (sloane/skills/bank.py): his accounts, read-only, through SimpleFIN.
-- Idempotent, like 001-035. Synced every few hours; balances, transactions and holdings
-- are what the bank last said. Descriptions are the bank's words: stored untrusted.
-- Moving money is a hard line, and nothing here could: SimpleFIN is read-only.
create table if not exists bank_accounts (
  id               text        primary key,          -- SimpleFIN's account id
  org              text        not null default '',  -- the bank: "Chase", "Fidelity Investments"
  name             text        not null,
  currency         text        not null default 'USD',
  kind             text        not null default 'cash',
  balance_cents    bigint,
  available_cents  bigint,
  balance_at       timestamptz,
  seen_at          timestamptz not null default now(),
  constraint bank_accounts_kind_ok check (kind in ('cash', 'credit', 'investment', 'loan'))
);

create table if not exists bank_transactions (
  account_id    text        not null references bank_accounts (id) on delete cascade,
  id            text        not null,
  posted_on     date        not null,
  amount_cents  bigint      not null,               -- negative: money out
  description   text        not null default '',
  pending       boolean     not null default false,
  category      text        not null default 'other',
  trusted       boolean     not null default false,
  seen_at       timestamptz not null default now(),
  primary key (account_id, id)
);

create index if not exists bank_transactions_posted_idx on bank_transactions (posted_on desc);

create table if not exists bank_holdings (
  account_id          text        not null references bank_accounts (id) on delete cascade,
  id                  text        not null,
  symbol              text,
  description         text        not null default '',
  shares              numeric,
  market_value_cents  bigint,
  cost_basis_cents    bigint,
  seen_at             timestamptz not null default now(),
  primary key (account_id, id)
);

-- Each account's balance at each day's last sync: the lines on the Money and Portfolio
-- widgets. SimpleFIN has no history of its own, so this starts the day he connects.
create table if not exists bank_history (
  account_id     text   not null references bank_accounts (id) on delete cascade,
  on_day         date   not null,
  balance_cents  bigint not null,
  primary key (account_id, on_day)
);

-- The sync: 6:10 AM to 9:10 PM, every three hours (SimpleFIN asks for 24 requests a day at most;
-- this is six). Silent: nothing is said, the numbers are just there. No model call, ever.
insert into jobs (name, cron) values ('bank_sync', '10 6-21/3 * * *')
on conflict (name) do nothing;
