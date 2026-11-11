#!/usr/bin/env python3
"""Scripted checks for supabase-sessions.sql against a local PostgreSQL 16.

Runs every call the way PostgREST would: one transaction per call, as role anon
(phones) or authenticated (the deck) with request.jwt.claim.sub set for the deck.
Usage: python3 test_sessions_local.py "dbname=llpoll user=postgres host=/var/run/postgresql"
"""
import hashlib, json, secrets, sys, uuid
import psycopg2, psycopg2.extras

DSN = sys.argv[1] if len(sys.argv) > 1 else "dbname=llpoll user=postgres host=/var/run/postgresql"
conn = psycopg2.connect(DSN)
conn.autocommit = True
psycopg2.extras.register_uuid()

results = []
def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (("  -- " + str(detail)) if detail and not ok else ""))

class Fail(Exception):
    pass

def call(role, sub, sql, params=()):
    """One PostgREST-style transaction. Returns rows (list of tuples) or raises Fail(message)."""
    cur = conn.cursor()
    try:
        cur.execute("begin")
        cur.execute("set local role " + role)
        cur.execute("select set_config('request.jwt.claim.sub', %s, true)", (sub or "",))
        cur.execute(sql, params)
        rows = cur.fetchall() if cur.description else []
        cur.execute("commit")
        return rows
    except psycopg2.Error as e:
        cur.execute("rollback")
        raise Fail((e.diag.message_primary or str(e)).strip())
    finally:
        cur.close()

def rpc(role, sub, fn, **kw):
    keys = list(kw)
    sql = "select * from public.%s(%s)" % (fn, ", ".join("%s := %%s" % k for k in keys))
    JSONB = {"p_polls", "p_answers"}
    rows = call(role, sub, sql, tuple(psycopg2.extras.Json(kw[k]) if k in JSONB else kw[k] for k in keys))
    return rows

def one(role, sub, fn, **kw):
    rows = rpc(role, sub, fn, **kw)
    return rows[0][0] if rows and len(rows[0]) == 1 else (rows[0] if rows else None)

def expect_error(name, fragment, fn):
    try:
        fn()
        check(name, False, "no error raised")
    except Fail as e:
        check(name, fragment.lower() in str(e).lower(), "got: " + str(e))

def admin(sql, params=()):
    cur = conn.cursor()
    cur.execute(sql, params)
    rows = cur.fetchall() if cur.description else []
    cur.close()
    return rows

def sha(token):
    return hashlib.sha256(token.encode()).hexdigest()

# ---------- fixtures ----------
OWNER_A, OWNER_B = str(uuid.uuid4()), str(uuid.uuid4())
admin("insert into auth.users (id) values (%s), (%s) on conflict do nothing", (OWNER_A, OWNER_B))
admin("delete from realtime.messages")
P1, P2, OUT = secrets.token_hex(32), secrets.token_hex(32), secrets.token_hex(32)
AUTH, ANON = "authenticated", "anon"

Q_CHOICE = [{"type": "choice", "prompt": "AI performs predictably across similar tasks.", "options": ["Agree", "Unsure", "Disagree"]},
            {"type": "choice", "prompt": "When AI increases performance, it also improves skill.", "options": ["Agree", "Unsure", "Disagree"]},
            {"type": "choice", "prompt": "AI productivity is the time saved generating an output.", "options": ["Agree", "Unsure", "Disagree"]}]
Q_RANK = [{"type": "ranking", "prompt": "Rank these tasks from hardest to easiest for AI.",
           "options": ["Count the r's in strawberry", "Reply to an email", "Summarize text", "Generate code", "Write an essay"]}]
Q_RATE = [{"type": "rating", "prompt": "Frontier", "min": 1, "max": 3, "labels": {"min": "Low", "max": "High"}},
          {"type": "rating", "prompt": "Learning", "min": 1, "max": 3},
          {"type": "rating", "prompt": "Oversight", "min": 1, "max": 3},
          {"type": "rating", "prompt": "Worst-Case", "min": 1, "max": 3}]
Q_TEXT = [{"type": "text", "prompt": "What could you include when sharing AI-generated work?", "maxEntries": 3}]
POLLS = [{"slide_key": "misconceptions", "questions": Q_CHOICE},
         {"slide_key": "jagged", "questions": Q_RANK},
         {"slide_key": "flow", "questions": Q_RATE},
         {"slide_key": "preface", "questions": Q_TEXT}]

# ---------- 1. functions exist with the right grants ----------
EXPECT = {
    "start_session": (False, True), "open_poll": (False, True), "close_poll": (False, True),
    "reveal_questions": (False, True), "hide_text_entry": (False, True),
    "join_session": (True, True), "session_state": (True, True), "save_answers": (True, True),
    "add_text_entry": (True, True), "remove_text_entry": (True, True), "poll_results": (True, True),
    "owned_live_poll": (False, False), "open_poll_for_vote": (False, False), "clean_answer": (False, False),
    "device_hash": (False, False), "is_device_participant": (False, False), "mask_text_entry": (False, False),
    "broadcast_poll_change": (False, False), "valid_questions": (False, True), "is_session_owner": (False, True),
    "poll_session": (False, True),
}
for fn, (anon_ok, auth_ok) in EXPECT.items():
    rows = admin("""select p.oid::regprocedure::text from pg_proc p join pg_namespace n on n.oid = p.pronamespace
                    where n.nspname = 'public' and p.proname = %s""", (fn,))
    check("function exists: " + fn, len(rows) == 1, rows)
    if rows:
        sig = rows[0][0]
        a = admin("select has_function_privilege('anon', %s, 'execute')", (sig,))[0][0]
        b = admin("select has_function_privilege('authenticated', %s, 'execute')", (sig,))[0][0]
        check("grants %s: anon=%s authenticated=%s" % (fn, anon_ok, auth_ok), a == anon_ok and b == auth_ok, (a, b))
for fn in ["claim_session", "rerun_poll"]:
    rows = admin("select 1 from pg_proc p join pg_namespace n on n.oid = p.pronamespace where n.nspname='public' and p.proname=%s", (fn,))
    check("cut function absent: " + fn, len(rows) == 0)

# ---------- 2. valid_questions rejects bad shapes ----------
def vq(q):
    return admin("select public.valid_questions(%s)", (psycopg2.extras.Json(q),))[0][0]
check("valid_questions accepts the 4 real polls", all(vq(p["questions"]) for p in POLLS))
bad = {
    "empty array": [],
    "6 questions": Q_CHOICE * 2,
    "not an array": {"type": "choice"},
    "missing prompt": [{"type": "choice", "options": ["a", "b"]}],
    "blank prompt": [{"type": "choice", "prompt": "  ", "options": ["a", "b"]}],
    "unknown type": [{"type": "slider", "prompt": "x"}],
    "choice 1 option": [{"type": "choice", "prompt": "x", "options": ["a"]}],
    "choice 9 options": [{"type": "choice", "prompt": "x", "options": list("abcdefghi")}],
    "choice non-string option": [{"type": "choice", "prompt": "x", "options": ["a", 2]}],
    "rating span 0": [{"type": "rating", "prompt": "x", "min": 3, "max": 3}],
    "rating span 10": [{"type": "rating", "prompt": "x", "min": 0, "max": 10}],
    "rating string min": [{"type": "rating", "prompt": "x", "min": "1", "max": 5}],
    "text maxEntries 11": [{"type": "text", "prompt": "x", "maxEntries": 11}],
    "text maxEntries string": [{"type": "text", "prompt": "x", "maxEntries": "3"}],
}
for name, q in bad.items():
    check("valid_questions rejects " + name, vq(q) is False)

# ---------- 3. start a session as owner A ----------
expect_error("start_session needs sign-in", "Authentication required", lambda: rpc(AUTH, None, "start_session", p_deck_key="Deck", p_polls=POLLS))
expect_error("start_session rejects a poll without slide_key", "slide_key", lambda: rpc(AUTH, OWNER_A, "start_session", p_deck_key="Deck", p_polls=[{"questions": Q_CHOICE}]))
expect_error("start_session rejects invalid questions", "invalid questions", lambda: rpc(AUTH, OWNER_A, "start_session", p_deck_key="Deck", p_polls=[{"slide_key": "x", "questions": []}]))
sess = rpc(AUTH, OWNER_A, "start_session", p_deck_key="Applying AI Discernment (120m) v1.3.0", p_polls=POLLS)[0]
SID, CODE, EXP = str(sess[0]), sess[1], sess[2]
check("start_session returns id, 6-char code, expiry", len(CODE) == 6 and EXP is not None, sess)
check("start_session returns no secret (cut)", len(sess) == 3, sess)
st = one(AUTH, OWNER_A, "session_state", p_session=SID)
check("owner state is waiting with 4 draft polls", st["state"] == "waiting" and st["is_owner"] and len(st["polls"]) == 4
      and all(p["status"] == "draft" for p in st["polls"]) and [p["slide_key"] for p in st["polls"]] == ["misconceptions", "jagged", "flow", "preface"], st)
check("no broadcast for drafts", admin("select count(*) from realtime.messages")[0][0] == 0)
polls = {p["slide_key"]: p["id"] for p in st["polls"]}
A, B, C, D = polls["misconceptions"], polls["jagged"], polls["flow"], polls["preface"]

# ---------- 4. phones join; outsiders and bad tokens are refused ----------
j = rpc(ANON, None, "join_session", p_code=CODE, p_token=P1)[0]
check("phone 1 joins by code", str(j[0]) == SID)
j2 = rpc(ANON, None, "join_session", p_code=CODE.lower(), p_token=P1)[0]
check("rejoining with the same token is idempotent", str(j2[0]) == SID and admin("select count(*) from public.session_participants where session_id=%s", (SID,))[0][0] == 1)
check("participant row holds a hash, not the token", admin("select device_hash from public.session_participants where session_id=%s", (SID,))[0][0] == sha(P1))
expect_error("bad token (63 chars)", "Invalid device token", lambda: rpc(ANON, None, "join_session", p_code=CODE, p_token=P1[:63]))
expect_error("bad token (uppercase)", "Invalid device token", lambda: rpc(ANON, None, "join_session", p_code=CODE, p_token=P1.upper()))
expect_error("unknown code", "not found", lambda: rpc(ANON, None, "join_session", p_code="ZZZZZZ", p_token=P2))
st = one(ANON, None, "session_state", p_session=SID, p_token=P1)
check("phone 1 sees waiting before the first launch", st["state"] == "waiting" and st["poll"] is None and st.get("polls") is None and st["is_owner"] is False, st)
check("outsider token gets ended", one(ANON, None, "session_state", p_session=SID, p_token=OUT)["state"] == "ended")
check("no token gets ended", one(ANON, None, "session_state", p_session=SID)["state"] == "ended")
for t in ["poll_sessions", "session_owners", "session_participants", "session_polls", "session_answers", "session_text_entries", "blocked_terms"]:
    expect_error("anon cannot select " + t, "permission denied", lambda t=t: call(ANON, None, "select * from public.%s" % t))
for fn, args in [("open_poll", "p_poll := %s"), ("close_poll", "p_poll := %s"), ("reveal_questions", "p_poll := %s, p_revealed := '{0}'"), ("hide_text_entry", "p_entry := %s, p_hidden := true")]:
    expect_error("anon cannot call " + fn, "permission denied", lambda fn=fn, args=args: call(ANON, None, "select public.%s(%s)" % (fn, args), (A,)))
expect_error("anon cannot call start_session", "permission denied", lambda: call(ANON, None, "select public.start_session('x', '[]'::jsonb)"))
check("anon cannot read blocked_terms", True)

# ---------- 5. owner B is a stranger to A's session ----------
check("owner B reads no session rows", call(AUTH, OWNER_B, "select id from public.poll_sessions") == [])
check("owner B reads no poll rows", call(AUTH, OWNER_B, "select id from public.session_polls") == [])
check("owner A reads own session rows", len(call(AUTH, OWNER_A, "select id from public.session_polls")) == 4)
expect_error("owner B cannot open A's poll", "Not allowed", lambda: rpc(AUTH, OWNER_B, "open_poll", p_poll=A))
check("owner B gets ended for A's session", one(AUTH, OWNER_B, "session_state", p_session=SID)["state"] == "ended")
expect_error("owner B cannot read A's results", "Not allowed", lambda: rpc(AUTH, OWNER_B, "poll_results", p_poll=A))
expect_error("phone cannot read results of a draft", "Not allowed", lambda: rpc(ANON, None, "poll_results", p_poll=A, p_token=P1))
expect_error("phone cannot vote on a draft", "not open", lambda: rpc(ANON, None, "save_answers", p_poll=A, p_answers={"0": {"choice": 0}}, p_token=P1))

# ---------- 6. launch A, vote, change, batch ----------
rpc(AUTH, OWNER_A, "open_poll", p_poll=A)
msgs = admin("select topic, event, payload, private from realtime.messages order by id")
check("launch broadcasts one public hint", len(msgs) == 1 and msgs[0][0] == "llpoll:" + CODE and msgs[0][1] == "poll-changed"
      and msgs[0][2]["status"] == "open" and msgs[0][3] is False, msgs)
st = one(ANON, None, "session_state", p_session=SID, p_token=P1)
check("phone 1 sees A open with 3 questions", st["state"] == "open" and st["poll"]["id"] == A and len(st["poll"]["questions"]) == 3 and st["poll"]["revealed"] == [], st)
r = one(ANON, None, "save_answers", p_poll=A, p_answers={"0": {"choice": 0}}, p_token=P1)
check("single answer saves", r["saved"] == 1, r)
r = one(ANON, None, "save_answers", p_poll=A, p_answers={"0": {"choice": 2}, "1": {"choice": 1}, "2": {"choice": 0}}, p_token=P1)
check("batch of 3 saves", r["saved"] == 3, r)
res = one(AUTH, OWNER_A, "poll_results", p_poll=A)
check("owner sees counts and 1 respondent after a change", res["respondents"] == 1 and res["questions"][0]["counts"] == [0, 0, 1]
      and res["questions"][1]["counts"] == [0, 1, 0] and res["questions"][2]["counts"] == [1, 0, 0], res)
expect_error("batch with one bad answer is rejected whole", "Bad answer", lambda: rpc(ANON, None, "save_answers", p_poll=A, p_answers={"0": {"choice": 1}, "1": {"choice": 7}}, p_token=P1))
res = one(AUTH, OWNER_A, "poll_results", p_poll=A)
check("rejected batch changed nothing", res["questions"][0]["counts"] == [0, 0, 1], res)
expect_error("answer with no such question", "No such question", lambda: rpc(ANON, None, "save_answers", p_poll=A, p_answers={"3": {"choice": 1}}, p_token=P1))
expect_error("answer with wrong shape", "Bad answer", lambda: rpc(ANON, None, "save_answers", p_poll=A, p_answers={"0": {"value": 1}}, p_token=P1))
expect_error("answers must be an object", "Bad answers", lambda: rpc(ANON, None, "save_answers", p_poll=A, p_answers=[1, 2], p_token=P1))
expect_error("outsider cannot vote", "not open", lambda: rpc(ANON, None, "save_answers", p_poll=A, p_answers={"0": {"choice": 1}}, p_token=OUT))
expect_error("text entry on a choice question", "Not a text question", lambda: rpc(ANON, None, "add_text_entry", p_poll=A, p_q=0, p_body="hi", p_token=P1))
expect_error("phone cannot read results while open", "Not allowed", lambda: rpc(ANON, None, "poll_results", p_poll=A, p_token=P1))
st = one(ANON, None, "session_state", p_session=SID, p_token=P1)
check("phone 1 gets its own answers back", st["mine"]["answers"] == {"0": {"choice": 2}, "1": {"choice": 1}, "2": {"choice": 0}}, st["mine"])

# ---------- 7. close A, reveal one at a time ----------
rpc(AUTH, OWNER_A, "close_poll", p_poll=A)
check("close broadcasts", admin("select payload->>'status' from realtime.messages order by id desc limit 1")[0][0] == "closed")
expect_error("vote after close is rejected", "not open", lambda: rpc(ANON, None, "save_answers", p_poll=A, p_answers={"0": {"choice": 1}}, p_token=P1))
expect_error("close twice", "not open", lambda: rpc(AUTH, OWNER_A, "close_poll", p_poll=A))
st = one(ANON, None, "session_state", p_session=SID, p_token=P1)
check("phone 1 sees closed", st["state"] == "closed" and st["poll"]["id"] == A, st)
res = one(ANON, None, "poll_results", p_poll=A, p_token=P1)
check("phone sees nothing before reveal", all(q.get("revealed") is False and "counts" not in q for q in res["questions"]), res)
expect_error("reveal out of range", "out of range", lambda: rpc(AUTH, OWNER_A, "reveal_questions", p_poll=A, p_revealed=[3]))
rpc(AUTH, OWNER_A, "reveal_questions", p_poll=A, p_revealed=[0])
check("reveal broadcasts revealed list", admin("select payload->'revealed' from realtime.messages order by id desc limit 1")[0][0] == [0])
res = one(ANON, None, "poll_results", p_poll=A, p_token=P1)
check("phone sees only question 1 after first reveal", res["questions"][0]["counts"] == [0, 0, 1] and res["questions"][1]["revealed"] is False and "counts" not in res["questions"][1], res)
rpc(AUTH, OWNER_A, "reveal_questions", p_poll=A, p_revealed=[0, 1, 2])
res = one(ANON, None, "poll_results", p_poll=A, p_token=P1)
check("phone sees all 3 after full reveal", all("counts" in q for q in res["questions"]) and res["respondents"] == 1, res)
rpc(AUTH, OWNER_A, "reveal_questions", p_poll=A, p_revealed=[])
res = one(ANON, None, "poll_results", p_poll=A, p_token=P1)
check("hide results hides again", all("counts" not in q for q in res["questions"]), res)
expect_error("outsider cannot read closed results", "Not allowed", lambda: rpc(ANON, None, "poll_results", p_poll=A, p_token=OUT))
expect_error("reveal on an open poll", "Close the poll", lambda: (rpc(AUTH, OWNER_A, "open_poll", p_poll=A), rpc(AUTH, OWNER_A, "reveal_questions", p_poll=A, p_revealed=[0])))
st = one(ANON, None, "session_state", p_session=SID, p_token=P1)
check("reopen keeps answers and clears reveal", st["state"] == "open" and st["mine"]["answers"]["0"] == {"choice": 2} and st["poll"]["revealed"] == [], st)

# ---------- 8. launch B while A is open closes A; late joiner ----------
rpc(AUTH, OWNER_A, "open_poll", p_poll=B)
st = one(AUTH, OWNER_A, "session_state", p_session=SID)
statuses = {p["slide_key"]: p["status"] for p in st["polls"]}
check("launching B closes A first", statuses == {"misconceptions": "closed", "jagged": "open", "flow": "draft", "preface": "draft"}, statuses)
check("only one open poll is possible (partial unique index)", admin("select count(*) from pg_indexes where indexname='session_polls_one_open'")[0][0] == 1)
rpc(ANON, None, "join_session", p_code=CODE, p_token=P2)
st = one(ANON, None, "session_state", p_session=SID, p_token=P2)
check("late joiner lands on the open poll B, not A", st["state"] == "open" and st["poll"]["id"] == B, st)
# ranking
expect_error("ranking must be a full permutation", "Bad answer", lambda: rpc(ANON, None, "save_answers", p_poll=B, p_answers={"0": {"order": [0, 1, 2, 3, 3]}}, p_token=P1))
expect_error("ranking must have n items", "Bad answer", lambda: rpc(ANON, None, "save_answers", p_poll=B, p_answers={"0": {"order": [0, 1, 2]}}, p_token=P1))
expect_error("ranking rejects strings", "Bad answer", lambda: rpc(ANON, None, "save_answers", p_poll=B, p_answers={"0": {"order": ["0", "1", "2", "3", "4"]}}, p_token=P1))
one(ANON, None, "save_answers", p_poll=B, p_answers={"0": {"order": [4, 3, 2, 1, 0]}}, p_token=P1)
one(ANON, None, "save_answers", p_poll=B, p_answers={"0": {"order": [0, 4, 3, 2, 1]}}, p_token=P2)
rpc(AUTH, OWNER_A, "close_poll", p_poll=B)
rpc(AUTH, OWNER_A, "reveal_questions", p_poll=B, p_revealed=[0])
res = one(ANON, None, "poll_results", p_poll=B, p_token=P2)
# hand count: option 0 at positions 5 and 1 -> 3.0; option 1: 4,5 -> 4.5; option 2: 3,4 -> 3.5; option 3: 2,3 -> 2.5; option 4: 1,2 -> 1.5
check("average ranks match a hand count", [float(x) for x in res["questions"][0]["average_rank"]] == [3.0, 4.5, 3.5, 2.5, 1.5] and res["respondents"] == 2, res)

# ---------- 9. rating ----------
rpc(AUTH, OWNER_A, "open_poll", p_poll=C)
expect_error("rating below min", "Bad answer", lambda: rpc(ANON, None, "save_answers", p_poll=C, p_answers={"0": {"value": 0}}, p_token=P1))
expect_error("rating above max", "Bad answer", lambda: rpc(ANON, None, "save_answers", p_poll=C, p_answers={"0": {"value": 4}}, p_token=P1))
one(ANON, None, "save_answers", p_poll=C, p_answers={"0": {"value": 1}, "1": {"value": 3}, "2": {"value": 2}, "3": {"value": 3}}, p_token=P1)
one(ANON, None, "save_answers", p_poll=C, p_answers={"0": {"value": 3}, "1": {"value": 3}}, p_token=P2)
rpc(AUTH, OWNER_A, "close_poll", p_poll=C)
res = one(AUTH, OWNER_A, "poll_results", p_poll=C)
check("rating counts and mean", res["questions"][0]["counts"] == [1, 0, 1] and float(res["questions"][0]["mean"]) == 2.0
      and res["questions"][1]["counts"] == [0, 0, 2] and res["questions"][2]["answered"] == 1, res)

# ---------- 10. free text: filter, hide, remove, limits ----------
rpc(AUTH, OWNER_A, "open_poll", p_poll=D)
e1 = one(ANON, None, "add_text_entry", p_poll=D, p_q=0, p_body="  What the AI drafted and what I changed  ", p_token=P1)
e2 = one(ANON, None, "add_text_entry", p_poll=D, p_q=0, p_body="This is fucking useless", p_token=P1)
e3 = one(ANON, None, "add_text_entry", p_poll=D, p_q=0, p_body="Which parts to check first", p_token=P2)
e4 = one(ANON, None, "add_text_entry", p_poll=D, p_q=0, p_body="Shitty draft, sorry", p_token=P2)
expect_error("empty entry rejected", "1 to 280", lambda: rpc(ANON, None, "add_text_entry", p_poll=D, p_q=0, p_body="   ", p_token=P2))
expect_error("281-char entry rejected", "1 to 280", lambda: rpc(ANON, None, "add_text_entry", p_poll=D, p_q=0, p_body="x" * 281, p_token=P2))
one(ANON, None, "add_text_entry", p_poll=D, p_q=0, p_body="third", p_token=P1)
expect_error("entry limit (maxEntries 3) holds", "Entry limit", lambda: rpc(ANON, None, "add_text_entry", p_poll=D, p_q=0, p_body="fourth", p_token=P1))
res = one(AUTH, OWNER_A, "poll_results", p_poll=D)
ents = {e["id"]: e for e in res["questions"][0]["entries"]}
check("word filter masks whole words only (fucking, shitty)", ents[str(e2)]["masked"] is True and ents[str(e1)]["masked"] is False
      and ents[str(e4)]["masked"] is False and ents[str(e3)]["masked"] is False, ents)
check("entry body is trimmed", ents[str(e1)]["body"] == "What the AI drafted and what I changed")
check("owner sees masked entries with ids", all("id" in e and "masked" in e for e in ents.values()))
st = one(ANON, None, "session_state", p_session=SID, p_token=P1)
check("phone sees its own entries (3)", len(st["mine"]["entries"]) == 3 and st["mine"]["entries"][0]["id"] == str(e1), st["mine"])
expect_error("phone 2 cannot remove phone 1's entry", "Not allowed", lambda: rpc(ANON, None, "remove_text_entry", p_entry=e1, p_token=P2))
rpc(ANON, None, "remove_text_entry", p_entry=e1, p_token=P1)
n_before = admin("select count(*) from realtime.messages")[0][0]
rpc(AUTH, OWNER_A, "hide_text_entry", p_entry=e3, p_hidden=True)
check("hiding an entry broadcasts a hint", admin("select count(*) from realtime.messages")[0][0] == n_before + 1)
expect_error("owner B cannot hide A's entry", "Not allowed", lambda: rpc(AUTH, OWNER_B, "hide_text_entry", p_entry=e4, p_hidden=True))
rpc(AUTH, OWNER_A, "close_poll", p_poll=D)
expect_error("remove after close is rejected", "not open", lambda: rpc(ANON, None, "remove_text_entry", p_entry=e2, p_token=P1))
rpc(AUTH, OWNER_A, "reveal_questions", p_poll=D, p_revealed=[0])
res = one(ANON, None, "poll_results", p_poll=D, p_token=P2)
bodies = [e["body"] for e in res["questions"][0]["entries"]]
check("phones see only clean, unhidden, unremoved entries", bodies == ["Shitty draft, sorry", "third"] and all("id" not in e for e in res["questions"][0]["entries"]), bodies)
check("text respondents counted by device", res["questions"][0]["answered"] == 2 and res["respondents"] == 2, res)

# ---------- 11. session cap ----------
sess2 = rpc(AUTH, OWNER_A, "start_session", p_deck_key="Cap test", p_polls=[POLLS[0]])[0]
admin("update public.poll_sessions set max_participants = 2 where id = %s", (str(sess2[0]),))
rpc(ANON, None, "join_session", p_code=sess2[1], p_token=P1)
rpc(ANON, None, "join_session", p_code=sess2[1], p_token=P2)
expect_error("participant cap holds", "Session is full", lambda: rpc(ANON, None, "join_session", p_code=sess2[1], p_token=OUT))

# ---------- 12. expiry: clock set back 2 hours ----------
admin("alter table public.poll_sessions disable trigger all")
admin("update public.poll_sessions set created_at = created_at - interval '2 hours', expires_at = expires_at - interval '2 hours' where id = %s", (SID,))
admin("alter table public.poll_sessions enable trigger all")
expect_error("expired session rejects launch", "Session has ended", lambda: rpc(AUTH, OWNER_A, "open_poll", p_poll=A))
expect_error("expired session rejects votes", "not open", lambda: rpc(ANON, None, "save_answers", p_poll=B, p_answers={"0": {"order": [0, 1, 2, 3, 4]}}, p_token=P1))
expect_error("expired session rejects joins", "not found or ended", lambda: rpc(ANON, None, "join_session", p_code=CODE, p_token=OUT))
check("phones see ended after expiry", one(ANON, None, "session_state", p_session=SID, p_token=P1)["state"] == "ended")
check("owner sees ended after expiry", one(AUTH, OWNER_A, "session_state", p_session=SID)["state"] == "ended")
expect_error("expired session rejects reveal", "Session has ended", lambda: rpc(AUTH, OWNER_A, "reveal_questions", p_poll=D, p_revealed=[]))
res = one(AUTH, OWNER_A, "poll_results", p_poll=D)
check("owner can still read results after expiry", res["respondents"] == 2)
expect_error("no role can update sessions directly", "permission denied", lambda: call(AUTH, OWNER_A, "update public.poll_sessions set expires_at = now() + interval '9 hours' where id = %s", (SID,)))
expect_error("no role can delete answers directly", "permission denied", lambda: call(AUTH, OWNER_A, "delete from public.session_answers"))
check("two-hour check constraint exists", admin("select count(*) from pg_constraint where conname='session_lasts_two_hours'")[0][0] == 1)

# ---------- 13. realtime publication ----------
pub = sorted(r[0] for r in admin("select tablename from pg_publication_tables where pubname='supabase_realtime' and schemaname='public'"))
check("publication carries session_answers, session_polls, session_text_entries", all(t in pub for t in ["session_answers", "session_polls", "session_text_entries"]), pub)

# ---------- 14. the migration runs twice without error ----------
import subprocess
p = subprocess.run(["psql", "-v", "ON_ERROR_STOP=1", "-q", "-X", DSN, "-f", sys.argv[2] if len(sys.argv) > 2 else "supabase-sessions.sql"], capture_output=True, text=True)
check("migration is re-runnable", p.returncode == 0, p.stderr[-500:])

n_fail = sum(1 for _, ok, _ in results if not ok)
print("\n%d checks, %d failed" % (len(results), n_fail))
sys.exit(1 if n_fail else 0)
