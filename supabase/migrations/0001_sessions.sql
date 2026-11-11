-- supabase-sessions.sql
-- Session-based live polls with device tokens. Respondents never sign in: each phone
-- sends a random 64-character token, and the database stores only its SHA-256 hash.
-- Facilitators keep anonymous sign-in (one per deck) to own a session.
-- Adds new tables beside polls/responses (left untouched), so decks that use the
-- one-hour poll keep working. Safe to run more than once.
--
-- Version 1 scope (decided 2026-11-05):
--   - no recovery secret or claim_session: a session belongs to the browser that started it
--   - no "Run again": the run column and the archived status stay reserved for later
--   - 30-second heartbeat and legacy ?poll= mode stay (they live in the page, not here)

create extension if not exists pgcrypto with schema extensions;

-- ---------- tables ----------

create table if not exists public.poll_sessions (
  id uuid primary key default gen_random_uuid(),
  code text not null unique check (code ~ '^[A-HJKMNP-Z2-9]{6}$'),
  deck_key text not null check (char_length(deck_key) between 1 and 200),
  created_by uuid not null references auth.users(id) on delete cascade,
  max_participants int not null default 500 check (max_participants between 1 and 2000),
  created_at timestamptz not null default now(),
  expires_at timestamptz not null,
  constraint session_lasts_two_hours check (expires_at = created_at + interval '2 hours')
);

create table if not exists public.session_owners (
  session_id uuid not null references public.poll_sessions(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  added_at timestamptz not null default now(),
  primary key (session_id, user_id)
);

-- device_hash = hex SHA-256 of the phone's token; the token itself is never stored.
create table if not exists public.session_participants (
  session_id uuid not null references public.poll_sessions(id) on delete cascade,
  device_hash text not null check (device_hash ~ '^[0-9a-f]{64}$'),
  joined_at timestamptz not null default now(),
  primary key (session_id, device_hash)
);

-- 1 to 5 questions. Each has a type and a prompt.
--   choice, ranking: 2 to 8 string options
--   rating: integer min and max, 1 to 9 apart, optional labels {min, max}
--   text: optional maxEntries 1 to 10 (default 5)
create or replace function public.valid_questions(q jsonb)
returns boolean language plpgsql immutable as $$
declare item jsonb; lo int; hi int;
begin
  if coalesce(jsonb_typeof(q), '') <> 'array' or jsonb_array_length(q) not between 1 and 5 then
    return false;
  end if;
  for item in select value from jsonb_array_elements(q) loop
    if jsonb_typeof(item) <> 'object' then return false; end if;
    if coalesce(jsonb_typeof(item->'prompt'), '') <> 'string' or btrim(item->>'prompt') = '' then
      return false;
    end if;
    case item->>'type'
      when 'choice', 'ranking' then
        if coalesce(jsonb_typeof(item->'options'), '') <> 'array'
           or jsonb_array_length(item->'options') not between 2 and 8
           or exists (select 1 from jsonb_array_elements(item->'options') o
                      where jsonb_typeof(o) <> 'string' or btrim(o #>> '{}') = '') then
          return false;
        end if;
      when 'rating' then
        if coalesce(jsonb_typeof(item->'min'), '') <> 'number'
           or coalesce(jsonb_typeof(item->'max'), '') <> 'number' then return false; end if;
        lo := (item->>'min')::int; hi := (item->>'max')::int;
        if hi - lo not between 1 and 9 then return false; end if;
      when 'text' then
        if item ? 'maxEntries' and (coalesce(jsonb_typeof(item->'maxEntries'), '') <> 'number'
           or (item->>'maxEntries')::int not between 1 and 10) then return false; end if;
      else
        return false;
    end case;
  end loop;
  return true;
exception when others then
  return false;
end $$;

create table if not exists public.session_polls (
  id uuid primary key default gen_random_uuid(),
  session_id uuid not null references public.poll_sessions(id) on delete cascade,
  slide_key text not null check (char_length(slide_key) between 1 and 100),
  position int not null,
  run int not null default 1,
  questions jsonb not null check (public.valid_questions(questions)),
  status text not null default 'draft' check (status in ('draft', 'open', 'closed', 'archived')),
  revealed int[] not null default '{}',
  opened_at timestamptz,
  closed_at timestamptz,
  updated_at timestamptz not null default now(),
  unique (session_id, slide_key, run)
);
create unique index if not exists session_polls_one_open
  on public.session_polls(session_id) where status = 'open';
create index if not exists session_polls_session_idx on public.session_polls(session_id);

create table if not exists public.session_answers (
  poll_id uuid not null references public.session_polls(id) on delete cascade,
  question_index int not null check (question_index between 0 and 4),
  device_hash text not null,
  value jsonb not null,
  updated_at timestamptz not null default now(),
  primary key (poll_id, question_index, device_hash)
);

create table if not exists public.session_text_entries (
  id uuid primary key default gen_random_uuid(),
  poll_id uuid not null references public.session_polls(id) on delete cascade,
  question_index int not null check (question_index between 0 and 4),
  device_hash text not null,
  body text not null check (char_length(btrim(body)) between 1 and 280),
  masked boolean not null default false,
  hidden boolean not null default false,
  created_at timestamptz not null default now()
);
create index if not exists session_text_entries_poll_idx on public.session_text_entries(poll_id);

-- Word filter. Lowercase single words only, so terms can go into a regex safely.
-- Add or remove terms in the SQL editor; nothing outside this file reads the table.
create table if not exists public.blocked_terms (
  term text primary key check (term ~ '^[a-z]+$')
);

insert into public.blocked_terms (term) values
  ('fuck'), ('fucking'), ('fucker'), ('motherfucker'), ('shit'), ('bullshit'), ('bitch'),
  ('asshole'), ('cunt'), ('dick'), ('cock'), ('pussy'), ('whore'), ('slut'), ('bastard'),
  ('damn'), ('nigger'), ('nigga'), ('faggot'), ('retard'), ('retarded'), ('kike'), ('spic'),
  ('chink'), ('tranny'), ('twat'), ('wanker'), ('prick')
on conflict do nothing;

-- ---------- helpers ----------

-- Validates a phone token and returns its hash. Rejects anything but 64 hex characters.
create or replace function public.device_hash(p_token text)
returns text language plpgsql immutable set search_path = public, extensions as $$
begin
  if p_token is null or p_token !~ '^[0-9a-f]{64}$' then raise exception 'Invalid device token'; end if;
  return encode(digest(p_token, 'sha256'), 'hex');
end $$;

create or replace function public.is_session_owner(p_session uuid)
returns boolean language sql stable security definer set search_path = public as $$
  select auth.uid() is not null and exists (
    select 1 from session_owners o where o.session_id = p_session and o.user_id = auth.uid());
$$;

-- True when this device joined the session and the session is still live.
create or replace function public.is_device_participant(p_session uuid, p_hash text)
returns boolean language sql stable security definer set search_path = public as $$
  select exists (select 1 from session_participants sp
                 join poll_sessions s on s.id = sp.session_id
                 where sp.session_id = p_session and sp.device_hash = p_hash
                   and now() < s.expires_at);
$$;

create or replace function public.poll_session(p_poll uuid)
returns uuid language sql stable security definer set search_path = public as $$
  select session_id from session_polls where id = p_poll;
$$;

-- ---------- word filter ----------

create or replace function public.mask_text_entry()
returns trigger language plpgsql security definer set search_path = public as $$
begin
  new.masked := exists (select 1 from blocked_terms b
                        where lower(new.body) ~ ('\m' || b.term || '\M'));
  return new;
end $$;

drop trigger if exists mask_text_entry on public.session_text_entries;
create trigger mask_text_entry before insert on public.session_text_entries
  for each row execute function public.mask_text_entry();

-- ---------- broadcast to phones ----------
-- Sends a small hint on the public topic llpoll:<code> whenever a poll opens, closes, reveals,
-- or is touched (updated_at) because an entry was hidden.
-- Anyone can send on a public topic, so phones treat a message only as "refetch now",
-- never as data. A failed send never blocks the facilitator; the phones' timed refresh covers it.
create or replace function public.broadcast_poll_change()
returns trigger language plpgsql security definer set search_path = public as $$
declare v_code text;
begin
  if new.status = 'draft' then return new; end if;
  select code into v_code from poll_sessions where id = new.session_id;
  begin
    perform realtime.send(
      jsonb_build_object('poll_id', new.id, 'status', new.status, 'revealed', to_jsonb(new.revealed)),
      'poll-changed', 'llpoll:' || v_code, false);
  exception when others then
    null;
  end;
  return new;
end $$;

drop trigger if exists broadcast_poll_change on public.session_polls;
create trigger broadcast_poll_change
  after insert or update of status, revealed, updated_at on public.session_polls
  for each row execute function public.broadcast_poll_change();

-- ---------- RLS ----------
-- Phones (anon role) get no table access at all; they use the functions below.
-- Facilitators (authenticated) read their own sessions directly, for live counts.
-- Nobody updates or deletes rows directly: no grants, no policies.

alter table public.poll_sessions enable row level security;
alter table public.session_owners enable row level security;
alter table public.session_participants enable row level security;
alter table public.session_polls enable row level security;
alter table public.session_answers enable row level security;
alter table public.session_text_entries enable row level security;
alter table public.blocked_terms enable row level security;

revoke all on public.poll_sessions, public.session_owners, public.session_participants,
  public.session_polls, public.session_answers, public.session_text_entries,
  public.blocked_terms from anon, authenticated;

grant select on public.poll_sessions, public.session_owners, public.session_polls,
  public.session_answers, public.session_text_entries to authenticated;

drop policy if exists "owners read sessions" on public.poll_sessions;
create policy "owners read sessions" on public.poll_sessions
  for select to authenticated using (public.is_session_owner(id));

drop policy if exists "read own ownership" on public.session_owners;
create policy "read own ownership" on public.session_owners
  for select to authenticated using (user_id = (select auth.uid()));

drop policy if exists "owners read polls" on public.session_polls;
create policy "owners read polls" on public.session_polls
  for select to authenticated using (public.is_session_owner(session_id));

drop policy if exists "owners read answers" on public.session_answers;
create policy "owners read answers" on public.session_answers
  for select to authenticated using (public.is_session_owner(public.poll_session(poll_id)));

drop policy if exists "owners read entries" on public.session_text_entries;
create policy "owners read entries" on public.session_text_entries
  for select to authenticated using (public.is_session_owner(public.poll_session(poll_id)));

-- ---------- facilitator functions (signed-in deck) ----------

-- Starts a 2-hour session and registers every poll slide as a draft, in deck order.
-- p_polls: [{"slide_key": "misconceptions", "questions": [...]}, ...]
create or replace function public.start_session(p_deck_key text, p_polls jsonb)
returns table (session_id uuid, code text, expires_at timestamptz)
language plpgsql security definer set search_path = public, extensions as $$
#variable_conflict use_column
declare
  v_id uuid; v_code text; v_now timestamptz := now();
  p jsonb; i int := 0;
begin
  if auth.uid() is null then raise exception 'Authentication required'; end if;
  if coalesce(jsonb_typeof(p_polls), '') <> 'array' or jsonb_array_length(p_polls) not between 1 and 20 then
    raise exception 'Between 1 and 20 polls are required';
  end if;
  loop
    select string_agg(substr('ABCDEFGHJKMNPQRSTUVWXYZ23456789',
                             1 + (get_byte(gen_random_bytes(1), 0) % 31), 1), '')
      into v_code from generate_series(1, 6);
    exit when not exists (select 1 from poll_sessions s where s.code = v_code);
  end loop;
  insert into poll_sessions (code, deck_key, created_by, created_at, expires_at)
  values (v_code, p_deck_key, auth.uid(), v_now, v_now + interval '2 hours')
  returning id into v_id;
  insert into session_owners (session_id, user_id) values (v_id, auth.uid());
  for p in select value from jsonb_array_elements(p_polls) loop
    i := i + 1;
    if coalesce(btrim(p->>'slide_key'), '') = '' then raise exception 'Poll % has no slide_key', i; end if;
    if not valid_questions(p->'questions') then raise exception 'Poll % has invalid questions', p->>'slide_key'; end if;
    insert into session_polls (session_id, slide_key, position, questions)
    values (v_id, p->>'slide_key', i, p->'questions');
  end loop;
  return query select v_id, v_code, v_now + interval '2 hours';
end $$;

-- Shared guard: caller owns the poll's session and the session is live. Locks the poll row.
create or replace function public.owned_live_poll(p_poll uuid)
returns session_polls language plpgsql security definer set search_path = public as $$
declare r session_polls;
begin
  select * into r from session_polls where id = p_poll for update;
  if r.id is null or not is_session_owner(r.session_id) then raise exception 'Not allowed'; end if;
  if not exists (select 1 from poll_sessions where id = r.session_id and now() < expires_at) then
    raise exception 'Session has ended';
  end if;
  return r;
end $$;

-- Launch (draft -> open) or reopen (closed -> open, answers kept). Closes any other open poll first.
create or replace function public.open_poll(p_poll uuid)
returns void language plpgsql security definer set search_path = public as $$
declare r session_polls := owned_live_poll(p_poll);
begin
  if r.status not in ('draft', 'closed') then raise exception 'Poll cannot be opened from %', r.status; end if;
  update session_polls set status = 'closed', closed_at = now(), updated_at = now()
   where session_id = r.session_id and status = 'open';
  update session_polls set status = 'open', opened_at = now(), closed_at = null,
         revealed = '{}', updated_at = now()
   where id = p_poll;
end $$;

create or replace function public.close_poll(p_poll uuid)
returns void language plpgsql security definer set search_path = public as $$
declare r session_polls := owned_live_poll(p_poll);
begin
  if r.status <> 'open' then raise exception 'Poll is not open'; end if;
  update session_polls set status = 'closed', closed_at = now(), updated_at = now() where id = p_poll;
end $$;

-- Sets the full list of revealed question numbers (0-based). An empty array hides results.
create or replace function public.reveal_questions(p_poll uuid, p_revealed int[])
returns void language plpgsql security definer set search_path = public as $$
declare r session_polls := owned_live_poll(p_poll);
begin
  if r.status <> 'closed' then raise exception 'Close the poll before revealing results'; end if;
  if exists (select 1 from unnest(coalesce(p_revealed, '{}')) v
             where v < 0 or v >= jsonb_array_length(r.questions)) then
    raise exception 'Question number out of range';
  end if;
  update session_polls set revealed = coalesce(p_revealed, '{}'), updated_at = now() where id = p_poll;
end $$;

create or replace function public.hide_text_entry(p_entry uuid, p_hidden boolean)
returns void language plpgsql security definer set search_path = public as $$
declare v_session uuid;
begin
  select poll_session(poll_id) into v_session from session_text_entries where id = p_entry;
  if v_session is null or not is_session_owner(v_session) then raise exception 'Not allowed'; end if;
  update session_text_entries set hidden = coalesce(p_hidden, true) where id = p_entry;
  -- touch the poll so phones get a broadcast hint and refetch the visible entries
  update session_polls set updated_at = now() where id = (select poll_id from session_text_entries where id = p_entry);
end $$;

-- ---------- phone functions (no sign-in; every call carries the device token) ----------

create or replace function public.join_session(p_code text, p_token text)
returns table (session_id uuid, expires_at timestamptz)
language plpgsql security definer set search_path = public as $$
#variable_conflict use_column
declare s poll_sessions; h text := device_hash(p_token);
begin
  select * into s from poll_sessions where code = upper(btrim(p_code)) and now() < expires_at for update;
  if s.id is null then raise exception 'Session not found or ended'; end if;
  if not exists (select 1 from session_participants where session_id = s.id and device_hash = h) then
    if (select count(*) from session_participants where session_id = s.id) >= s.max_participants then
      raise exception 'Session is full';
    end if;
    insert into session_participants (session_id, device_hash) values (s.id, h);
  end if;
  return query select s.id, s.expires_at;
end $$;

-- What a screen should show. Phones pass their token; the deck passes none and must own the session.
create or replace function public.session_state(p_session uuid, p_token text default null)
returns jsonb language plpgsql stable security definer set search_path = public as $$
declare
  s poll_sessions; v_owner boolean := is_session_owner(p_session); cur session_polls;
  h text := case when p_token is null then null else device_hash(p_token) end;
  mine jsonb;
begin
  select * into s from poll_sessions where id = p_session;
  if s.id is null or not (v_owner or (h is not null and is_device_participant(p_session, h))) then
    return jsonb_build_object('state', 'ended');
  end if;
  select * into cur from session_polls
   where session_id = p_session and status in ('open', 'closed')
   order by (status = 'open') desc, coalesce(closed_at, opened_at) desc nulls last
   limit 1;
  if h is not null and cur.id is not null then
    mine := jsonb_build_object(
      'answers', coalesce((select jsonb_object_agg(question_index::text, value)
                           from session_answers where poll_id = cur.id and device_hash = h), '{}'::jsonb),
      'entries', coalesce((select jsonb_agg(jsonb_build_object('id', id, 'question', question_index, 'body', body, 'masked', masked)
                                            order by created_at)
                           from session_text_entries where poll_id = cur.id and device_hash = h), '[]'::jsonb));
  end if;
  return jsonb_build_object(
    'state', case when now() >= s.expires_at then 'ended' when cur.id is null then 'waiting' else cur.status end,
    'code', s.code,
    'expires_at', s.expires_at,
    'server_time', now(),
    'is_owner', v_owner,
    'poll', case when cur.id is null then null else jsonb_build_object(
      'id', cur.id, 'slide_key', cur.slide_key, 'position', cur.position, 'run', cur.run,
      'status', cur.status, 'questions', cur.questions, 'revealed', to_jsonb(cur.revealed)) end,
    'mine', mine,
    'polls', case when v_owner then (select jsonb_agg(jsonb_build_object(
      'id', p.id, 'slide_key', p.slide_key, 'position', p.position, 'run', p.run, 'status', p.status,
      'revealed', to_jsonb(p.revealed))
      order by p.position, p.run) from session_polls p where p.session_id = p_session) else null end
  );
end $$;

-- Shared guard for votes: poll is open, session live, device joined. Shares the poll row so a close can't race.
create or replace function public.open_poll_for_vote(p_poll uuid, p_q int, p_hash text)
returns jsonb language plpgsql security definer set search_path = public as $$
declare r session_polls;
begin
  select * into r from session_polls where id = p_poll for share;
  if r.id is null or r.status <> 'open' or not is_device_participant(r.session_id, p_hash) then
    raise exception 'Poll is not open';
  end if;
  if p_q is null or p_q < 0 or p_q >= jsonb_array_length(r.questions) then raise exception 'No such question'; end if;
  return r.questions->p_q;
end $$;

-- Validates one choice, rating or ranking answer against its question; returns the cleaned value.
create or replace function public.clean_answer(q jsonb, p_value jsonb)
returns jsonb language plpgsql immutable as $$
declare n int; v int;
begin
  case q->>'type'
    when 'choice' then
      n := jsonb_array_length(q->'options');
      if coalesce(jsonb_typeof(p_value->'choice'), '') <> 'number' then raise exception 'Bad answer'; end if;
      v := (p_value->>'choice')::int;
      if v < 0 or v >= n then raise exception 'Bad answer'; end if;
      return jsonb_build_object('choice', v);
    when 'rating' then
      if coalesce(jsonb_typeof(p_value->'value'), '') <> 'number' then raise exception 'Bad answer'; end if;
      v := (p_value->>'value')::int;
      if v < (q->>'min')::int or v > (q->>'max')::int then raise exception 'Bad answer'; end if;
      return jsonb_build_object('value', v);
    when 'ranking' then
      n := jsonb_array_length(q->'options');
      if coalesce(jsonb_typeof(p_value->'order'), '') <> 'array'
         or jsonb_array_length(p_value->'order') <> n
         or exists (select 1 from jsonb_array_elements(p_value->'order') e where jsonb_typeof(e) <> 'number')
         or (select array_agg(e::int order by e::int) from jsonb_array_elements_text(p_value->'order') e)
            <> (select array_agg(g order by g) from generate_series(0, n - 1) g) then
        raise exception 'Bad answer';
      end if;
      return jsonb_build_object('order', (select jsonb_agg(e::int order by o)
                                          from jsonb_array_elements_text(p_value->'order') with ordinality t(e, o)));
    else
      raise exception 'Use add_text_entry for text questions';
  end case;
exception when invalid_text_representation or numeric_value_out_of_range then
  raise exception 'Bad answer';
end $$;

-- Batched autosave: the page sends every current choice, rating and ranking answer for one poll,
-- about half a second after the last change, as {"0": {...}, "2": {...}}. All or nothing.
create or replace function public.save_answers(p_poll uuid, p_answers jsonb, p_token text)
returns jsonb language plpgsql security definer set search_path = public as $$
declare h text := device_hash(p_token); r session_polls; k text; v jsonb; qi int; n int := 0;
begin
  if coalesce(jsonb_typeof(p_answers), '') <> 'object' then raise exception 'Bad answers'; end if;
  select * into r from session_polls where id = p_poll for share;
  if r.id is null or r.status <> 'open' or not is_device_participant(r.session_id, h) then
    raise exception 'Poll is not open';
  end if;
  for k, v in select key, value from jsonb_each(p_answers) loop
    if k !~ '^[0-4]$' or k::int >= jsonb_array_length(r.questions) then raise exception 'No such question'; end if;
    qi := k::int;
    insert into session_answers (poll_id, question_index, device_hash, value)
    values (p_poll, qi, h, clean_answer(r.questions->qi, v))
    on conflict (poll_id, question_index, device_hash)
    do update set value = excluded.value, updated_at = now();
    n := n + 1;
  end loop;
  return jsonb_build_object('saved', n, 'saved_at', now());
end $$;

create or replace function public.add_text_entry(p_poll uuid, p_q int, p_body text, p_token text)
returns uuid language plpgsql security definer set search_path = public as $$
declare h text := device_hash(p_token); q jsonb := open_poll_for_vote(p_poll, p_q, h); v_id uuid;
begin
  if q->>'type' <> 'text' then raise exception 'Not a text question'; end if;
  if char_length(btrim(coalesce(p_body, ''))) not between 1 and 280 then
    raise exception 'Entries are 1 to 280 characters';
  end if;
  if (select count(*) from session_text_entries
      where poll_id = p_poll and question_index = p_q and device_hash = h)
     >= coalesce((q->>'maxEntries')::int, 5) then
    raise exception 'Entry limit reached';
  end if;
  insert into session_text_entries (poll_id, question_index, device_hash, body)
  values (p_poll, p_q, h, btrim(p_body)) returning id into v_id;
  return v_id;
end $$;

create or replace function public.remove_text_entry(p_entry uuid, p_token text)
returns void language plpgsql security definer set search_path = public as $$
declare h text := device_hash(p_token); e session_text_entries;
begin
  select * into e from session_text_entries where id = p_entry and device_hash = h;
  if e.id is null then raise exception 'Not allowed'; end if;
  perform open_poll_for_vote(e.poll_id, e.question_index, h);
  delete from session_text_entries where id = p_entry;
end $$;

-- ---------- results (aggregates only) ----------
-- The deck (owner) sees everything at any time. A phone sees a closed poll's revealed questions only.

create or replace function public.poll_results(p_poll uuid, p_token text default null)
returns jsonb language plpgsql stable security definer set search_path = public as $$
declare
  r session_polls; v_owner boolean; q jsonb; qi int; v_out jsonb := '[]'::jsonb; item jsonb;
  respondents int; h text := case when p_token is null then null else device_hash(p_token) end;
begin
  select * into r from session_polls where id = p_poll;
  if r.id is null then raise exception 'Not found'; end if;
  v_owner := is_session_owner(r.session_id);
  if not v_owner and not (r.status = 'closed' and h is not null and is_device_participant(r.session_id, h)) then
    raise exception 'Not allowed';
  end if;

  select count(distinct device_hash) into respondents from (
    select device_hash from session_answers where poll_id = p_poll
    union all
    select device_hash from session_text_entries where poll_id = p_poll) x;

  for qi in 0 .. jsonb_array_length(r.questions) - 1 loop
    if not v_owner and not (qi = any (r.revealed)) then
      v_out := v_out || jsonb_build_array(jsonb_build_object('index', qi, 'revealed', false));
      continue;
    end if;
    q := r.questions->qi;
    item := jsonb_build_object('index', qi, 'revealed', qi = any (r.revealed), 'type', q->>'type',
      'answered', (select count(*) from session_answers where poll_id = p_poll and question_index = qi));
    case q->>'type'
      when 'choice' then
        item := item || jsonb_build_object('counts', (
          select jsonb_agg(coalesce(c.n, 0) order by g.k)
          from generate_series(0, jsonb_array_length(q->'options') - 1) g(k)
          left join (select (value->>'choice')::int k, count(*) n from session_answers
                     where poll_id = p_poll and question_index = qi group by 1) c using (k)));
      when 'rating' then
        item := item || jsonb_build_object('counts', (
          select jsonb_agg(coalesce(c.n, 0) order by g.k)
          from generate_series((q->>'min')::int, (q->>'max')::int) g(k)
          left join (select (value->>'value')::int k, count(*) n from session_answers
                     where poll_id = p_poll and question_index = qi group by 1) c using (k)),
          'mean', (select round(avg((value->>'value')::numeric), 2) from session_answers
                   where poll_id = p_poll and question_index = qi));
      when 'ranking' then
        item := item || jsonb_build_object('average_rank', (
          select jsonb_agg(t.avg_rank order by t.k)
          from (select g.k, round(avg(e.pos), 2) avg_rank
                from generate_series(0, jsonb_array_length(q->'options') - 1) g(k)
                left join (select (e.v)::int k, e.pos
                           from session_answers a,
                                jsonb_array_elements_text(a.value->'order') with ordinality e(v, pos)
                           where a.poll_id = p_poll and a.question_index = qi) e using (k)
                group by g.k) t));
      when 'text' then
        item := item || jsonb_build_object('answered', (select count(distinct device_hash)
          from session_text_entries where poll_id = p_poll and question_index = qi));
        item := item || jsonb_build_object('entries', coalesce((
          select jsonb_agg(case when v_owner
                   then jsonb_build_object('id', t.id, 'body', t.body, 'masked', t.masked, 'hidden', t.hidden)
                   else jsonb_build_object('body', t.body) end
                 order by t.created_at)
          from session_text_entries t
          where t.poll_id = p_poll and t.question_index = qi
            and (v_owner or (not t.masked and not t.hidden))), '[]'::jsonb));
    end case;
    v_out := v_out || jsonb_build_array(item);
  end loop;

  return jsonb_build_object('poll_id', p_poll, 'status', r.status, 'respondents', respondents,
                            'revealed', to_jsonb(r.revealed), 'questions', v_out);
end $$;

-- ---------- function grants ----------

revoke all on function
  public.valid_questions(jsonb), public.device_hash(text), public.is_session_owner(uuid),
  public.is_device_participant(uuid, text), public.poll_session(uuid),
  public.mask_text_entry(), public.broadcast_poll_change(),
  public.start_session(text, jsonb), public.owned_live_poll(uuid),
  public.open_poll(uuid), public.close_poll(uuid), public.reveal_questions(uuid, int[]),
  public.hide_text_entry(uuid, boolean),
  public.join_session(text, text), public.session_state(uuid, text),
  public.open_poll_for_vote(uuid, int, text), public.clean_answer(jsonb, jsonb),
  public.save_answers(uuid, jsonb, text),
  public.add_text_entry(uuid, int, text, text), public.remove_text_entry(uuid, text),
  public.poll_results(uuid, text)
from public, anon, authenticated;

-- RLS policies call these as the signed-in facilitator.
grant execute on function public.valid_questions(jsonb), public.is_session_owner(uuid),
  public.poll_session(uuid) to authenticated;

-- Facilitator API (signed-in deck only).
grant execute on function
  public.start_session(text, jsonb),
  public.open_poll(uuid), public.close_poll(uuid), public.reveal_questions(uuid, int[]),
  public.hide_text_entry(uuid, boolean)
to authenticated;

-- Phone API (no sign-in). Also granted to authenticated so a facilitator's browser can vote in tests.
grant execute on function
  public.join_session(text, text), public.session_state(uuid, text),
  public.save_answers(uuid, jsonb, text), public.add_text_entry(uuid, int, text, text),
  public.remove_text_entry(uuid, text), public.poll_results(uuid, text)
to anon, authenticated;

-- ---------- realtime ----------
-- Phones use the public broadcast above. Only the deck listens to row changes, for live counts.

do $$
declare t text;
begin
  foreach t in array array['session_answers', 'session_text_entries', 'session_polls'] loop
    if not exists (select 1 from pg_publication_tables
                   where pubname = 'supabase_realtime' and schemaname = 'public' and tablename = t) then
      execute format('alter publication supabase_realtime add table public.%I', t);
    end if;
  end loop;
end $$;

-- ---------- retention (run on a schedule, e.g. daily with pg_cron; not enabled here) ----------
-- delete from public.poll_sessions where expires_at < now() - interval '30 days';
