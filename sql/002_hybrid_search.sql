-- Hybrid retrieval and memory provenance. Idempotent, like 001.
--
-- Two changes, both of which fix a real hole rather than adding a feature.
--
-- 1. Full-text search alongside the vectors. Pure vector recall blurs exactly
--    the tokens that matter most here -- "DECA", "Babcock", "Jewelry I",
--    "Keegan" -- because a rare proper noun is precisely what an embedding
--    averages away and what BM25-style ranking weights most heavily. Published
--    comparisons put hybrid retrieval well ahead of either arm alone. Postgres
--    does this natively, so it costs no new dependency.
--
--    It also means recall no longer depends on the embedder existing at all:
--    if the ONNX weights are missing, the full-text arm still answers.
--
-- 2. Provenance. An episode that came out of an email is not something Landen
--    said. Without a flag, ingested text is fenced as untrusted on the turn it
--    arrives and then, once embedded, silently re-enters later prompts as
--    RECALL with the label gone. That turns a one-shot injection into a
--    standing instruction. `trusted` keeps the label attached to the row.

alter table episodes add column if not exists trusted boolean not null default true;
alter table episodes add column if not exists source text;

-- Generated, so it can never drift from the text it indexes. The two-argument
-- to_tsvector is immutable, which is what makes it legal in a stored column.
alter table episodes add column if not exists tsv tsvector
  generated always as (
    to_tsvector('english', coalesce(summary, '') || ' ' || content)
  ) stored;

create index if not exists episodes_tsv_idx on episodes using gin (tsv);

-- Recall reads trusted rows almost every time; keep that path cheap.
create index if not exists episodes_trusted_idx
  on episodes (occurred_at desc) where trusted;

comment on column episodes.trusted is
  'False for text Landen did not write (email bodies, portal HTML, calendar '
  'descriptions). Untrusted rows stay out of RECALL unless explicitly asked '
  'for, and are fenced when they are included.';
