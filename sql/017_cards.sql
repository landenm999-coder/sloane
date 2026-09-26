-- The cards skill (sloane/skills/cards.py): flashcards on a Leitner schedule,
-- reviewed in /quiz sessions. Idempotent, like 001-016.
--
-- box 1-6; a right answer moves a card up a box and pushes due_on out
-- (1, 2, 4, 8, 16, 32 days), a miss sends it back to box 1. Dropped cards are
-- archived, never deleted.
create table if not exists cards (
  id           uuid primary key default gen_random_uuid(),
  deck         text        not null,
  front        text        not null,
  back         text        not null,
  box          integer     not null default 1,
  due_on       date        not null,
  reviews      integer     not null default 0,
  lapses       integer     not null default 0,
  created_at   timestamptz not null default now(),
  reviewed_at  timestamptz,
  archived_at  timestamptz,
  constraint cards_box_ok check (box between 1 and 6)
);

create index if not exists cards_due_idx on cards (due_on) where archived_at is null;
-- The same question twice in one deck is one card.
create unique index if not exists cards_deck_front_key
  on cards (deck, lower(front)) where archived_at is null;

create table if not exists card_reviews (
  id       bigserial primary key,
  card_id  uuid        not null references cards (id) on delete cascade,
  at       timestamptz not null default now(),
  correct  boolean     not null
);

create index if not exists card_reviews_at_idx on card_reviews (at desc);
