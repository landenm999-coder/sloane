-- Where an outbound message came from. Idempotent, like 001-024.
--
-- A reply built from outside text (an email summary, an answer from web
-- search results) is marked untrusted. CONVERSATION shows it only as a
-- placeholder, so strangers' words never ride unfenced into a turn that can
-- act for him (sloane/memory/tiers.py render_conversation).
alter table messages add column if not exists trusted boolean not null default true;
