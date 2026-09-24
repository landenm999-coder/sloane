-- The lists skill (sloane/skills/lists.py): grocery, packing, to-do -- any
-- list he names. Idempotent, like 001-014.
--
-- Items are never deleted. Checking one off (or clearing the list) sets
-- done_at, so "what did I have on the packing list last time?" stays answerable
-- and nothing he wrote is lost to a mistyped "clear".
create table if not exists list_items (
  id        uuid primary key default gen_random_uuid(),
  list      text        not null,
  item      text        not null,
  added_at  timestamptz not null default now(),
  done_at   timestamptz
);

create index if not exists list_items_open_idx
  on list_items (list, added_at) where done_at is null;
