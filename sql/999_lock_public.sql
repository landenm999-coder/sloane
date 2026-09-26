-- Close Supabase's automatic web API over her tables.
--
-- Supabase publishes every table in `public` through its REST API (PostgREST,
-- reachable with the project's anon key), and its security advisor emails the
-- owner about each table without row-level security. Sloane never uses that
-- API: she connects as the owner of her tables, and row-level security does not
-- restrict a table's owner. So RLS on with no policies closes the API to every
-- other role and changes nothing she does. On plain Postgres (local, CI) the
-- anon and authenticated roles don't exist and only the RLS half applies.
--
-- Numbered 999 so it sorts after every migration, and every install and
-- upgrade re-applies them all: a table added later is covered the next time.
-- Idempotent.

do $$
declare
  t record;
  api_role text;
begin
  for t in
    select c.relname
      from pg_class c
      join pg_namespace n on n.oid = c.relnamespace
     where n.nspname = 'public' and c.relkind in ('r', 'p') and not c.relrowsecurity
  loop
    execute format('alter table public.%I enable row level security', t.relname);
  end loop;

  foreach api_role in array array['anon', 'authenticated'] loop
    if exists (select 1 from pg_roles where rolname = api_role) then
      execute format('revoke all on all tables in schema public from %I', api_role);
      execute format('revoke all on all sequences in schema public from %I', api_role);
      execute format('alter default privileges in schema public revoke all on tables from %I', api_role);
      execute format('alter default privileges in schema public revoke all on sequences from %I', api_role);
    end if;
  end loop;
end $$;
