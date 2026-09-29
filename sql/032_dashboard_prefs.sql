-- The control room's panels (sloane/web.py): which are on, which are off, and
-- the three the phone shows. One row, so the choices follow him from the
-- laptop to the phone. Idempotent, like 001-031.
--
-- A panel in neither list takes its default (sloane/web.py PANELS), so a panel
-- added later turns up without anyone having to switch it on.
create table if not exists dashboard_prefs (
  id          smallint primary key default 1 check (id = 1),
  shown       text[] not null default '{}',
  hidden      text[] not null default '{}',
  phone       text[] not null default '{}',
  updated_at  timestamptz not null default now()
);
