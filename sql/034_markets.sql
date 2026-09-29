-- The markets skill (sloane/skills/markets.py): the tickers he watches. Idempotent,
-- like 001-033. Prices are never stored; they are fetched (and cached in memory)
-- when asked. An empty list means the broad market: the S&P 500, the Nasdaq, Bitcoin.
create table if not exists watchlist (
  symbol    text        primary key,
  added_at  timestamptz not null default now(),
  constraint watchlist_symbol_ok check (symbol ~ '^[A-Z0-9^][A-Z0-9.=^-]{0,14}$')
);
