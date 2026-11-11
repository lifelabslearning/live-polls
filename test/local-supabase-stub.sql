-- Stand-ins for the parts of Supabase that supabase-sessions.sql depends on,
-- so the migration can be tested on a plain PostgreSQL 16. Not for Supabase itself.

create extension if not exists pgcrypto;
create schema if not exists extensions;
create extension if not exists pgcrypto with schema extensions;

-- Roles Supabase provides.
do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'anon') then create role anon nologin; end if;
  if not exists (select 1 from pg_roles where rolname = 'authenticated') then create role authenticated nologin; end if;
end $$;
grant usage on schema public to anon, authenticated;
grant usage on schema extensions to anon, authenticated;

-- auth schema: users table and auth.uid() reading the JWT claim the way Supabase does.
create schema if not exists auth;
create table if not exists auth.users (id uuid primary key, created_at timestamptz default now());
create or replace function auth.uid() returns uuid language sql stable as $$
  select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid;
$$;
grant usage on schema auth to anon, authenticated;
grant execute on function auth.uid() to anon, authenticated;

-- realtime.send stand-in: records every broadcast so tests can count them.
create schema if not exists realtime;
create table if not exists realtime.messages (
  id bigserial primary key, topic text, event text, payload jsonb, private boolean, sent_at timestamptz default now());
create or replace function realtime.send(payload jsonb, event text, topic text, private boolean default true)
returns void language sql as $$
  insert into realtime.messages (topic, event, payload, private) values (topic, event, payload, private);
$$;

-- The publication the migration adds tables to.
do $$ begin
  if not exists (select 1 from pg_publication where pubname = 'supabase_realtime') then
    create publication supabase_realtime;
  end if;
end $$;
