#!/usr/bin/env python3
"""The same checks as test_sessions_local.py, run against a real Supabase project over
its REST API (PostgREST + GoTrue) and a Realtime websocket, exactly as the pages will.

Usage: python3 test_sessions_rest.py https://<ref>.supabase.co sb_publishable_...
Needs only the publishable key. Creates 2 anonymous facilitator users (2 of the
30-per-hour anonymous sign-ins allowed per IP) and a few test sessions.
"""
import asyncio, hashlib, json, secrets, sys, time
import requests

URL, KEY = sys.argv[1].rstrip("/"), sys.argv[2]
REST = URL + "/rest/v1"
results = []

def check(name, ok, detail=""):
    results.append((name, ok))
    print(("PASS " if ok else "FAIL ") + name + (("  -- " + str(detail)[:300]) if detail and not ok else ""))

class Fail(Exception):
    pass

def headers(jwt=None):
    return {"apikey": KEY, "Authorization": "Bearer " + (jwt or KEY), "Content-Type": "application/json"}

def rpc(jwt, fn, **params):
    r = requests.post(REST + "/rpc/" + fn, headers=headers(jwt), data=json.dumps(params), timeout=20)
    if r.status_code >= 400:
        try:
            msg = r.json().get("message") or r.text
        except Exception:
            msg = r.text
        raise Fail("%s: %s" % (r.status_code, msg))
    return r.json() if r.text else None

def select(jwt, table, query="select=*"):
    r = requests.get(REST + "/" + table + "?" + query, headers=headers(jwt), timeout=20)
    if r.status_code >= 400:
        raise Fail("%s: %s" % (r.status_code, r.text))
    return r.json()

def write(jwt, method, table, body=None, query=""):
    r = requests.request(method, REST + "/" + table + ("?" + query if query else ""), headers=headers(jwt), data=json.dumps(body or {}), timeout=20)
    if r.status_code >= 400:
        raise Fail("%s: %s" % (r.status_code, r.text))
    return r

def anon_user():
    r = requests.post(URL + "/auth/v1/signup", headers={"apikey": KEY, "Content-Type": "application/json"},
                      data=json.dumps({"data": {}, "gotrue_meta_security": {}}), timeout=20)
    if r.status_code >= 400:
        raise SystemExit("anonymous sign-in failed: %s %s" % (r.status_code, r.text))
    j = r.json()
    return j["access_token"], j["user"]["id"]

def expect_error(name, fragment, fn):
    try:
        fn()
        check(name, False, "no error raised")
    except Fail as e:
        check(name, fragment.lower() in str(e).lower(), "got: " + str(e))

# ---------- Realtime broadcast listener ----------
async def listen_broadcasts(code, seconds, out):
    import websockets
    ws_url = URL.replace("https://", "wss://") + "/realtime/v1/websocket?apikey=" + KEY + "&vsn=1.0.0"
    async with websockets.connect(ws_url, max_size=2 ** 20) as ws:
        topic = "realtime:llpoll:" + code
        await ws.send(json.dumps({"topic": topic, "event": "phx_join", "payload": {"config": {"broadcast": {"self": False}, "presence": {"key": ""}, "private": False}}, "ref": "1"}))
        end = time.time() + seconds
        joined = False
        while time.time() < end:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, end - time.time()))
            except asyncio.TimeoutError:
                break
            m = json.loads(raw)
            if m.get("event") == "phx_reply" and m.get("ref") == "1":
                joined = m["payload"].get("status") == "ok"
                out["joined"] = joined
                out["join_reply"] = m["payload"]
            elif m.get("event") == "broadcast":
                out.setdefault("messages", []).append(m["payload"])
            if time.time() > end - 0.2 and joined is False:
                break

def run_with_listener(code, actions, wait=4.0):
    """Run `actions` (a callable) while a websocket listens on llpoll:<code>; return collected broadcasts."""
    out = {}
    async def main():
        task = asyncio.create_task(listen_broadcasts(code, wait + 3, out))
        await asyncio.sleep(2.5)  # time to join
        await asyncio.get_event_loop().run_in_executor(None, actions)
        await task
    asyncio.run(main())
    return out

# ---------- fixtures ----------
JWT_A, OWNER_A = anon_user()
JWT_B, OWNER_B = anon_user()
P1, P2, OUT = secrets.token_hex(32), secrets.token_hex(32), secrets.token_hex(32)
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
POLLS = [{"slide_key": "misconceptions", "questions": Q_CHOICE}, {"slide_key": "jagged", "questions": Q_RANK},
         {"slide_key": "flow", "questions": Q_RATE}, {"slide_key": "preface", "questions": Q_TEXT}]

# ---------- 1. grants as seen through PostgREST (exact parameter names, or PostgREST answers 404 first) ----------
Z = "00000000-0000-0000-0000-000000000000"
SIG = {"open_poll": {"p_poll": Z}, "close_poll": {"p_poll": Z}, "reveal_questions": {"p_poll": Z, "p_revealed": [0]},
       "hide_text_entry": {"p_entry": Z, "p_hidden": True}, "start_session": {"p_deck_key": "x", "p_polls": []},
       "owned_live_poll": {"p_poll": Z}, "open_poll_for_vote": {"p_poll": Z, "p_q": 0, "p_hash": "x"}, "clean_answer": {"q": {}, "p_value": {}},
       "device_hash": {"p_token": "x"}, "is_device_participant": {"p_session": Z, "p_hash": "x"}, "valid_questions": {"q": []},
       "is_session_owner": {"p_session": Z}, "poll_session": {"p_poll": Z}}
for fn, args in SIG.items():
    expect_error("anon cannot call " + fn, "permission denied", lambda fn=fn, args=args: rpc(None, fn, **args))
for fn in ["owned_live_poll", "open_poll_for_vote", "clean_answer", "device_hash", "is_device_participant"]:
    expect_error("authenticated cannot call helper " + fn, "permission denied", lambda fn=fn: rpc(JWT_A, fn, **SIG[fn]))
for fn in ["claim_session", "rerun_poll"]:
    expect_error("cut function absent: " + fn, "could not find", lambda fn=fn: rpc(JWT_A, fn, p_code="x", p_secret="x", p_poll="00000000-0000-0000-0000-000000000000"))
for t in ["poll_sessions", "session_owners", "session_participants", "session_polls", "session_answers", "session_text_entries", "blocked_terms"]:
    expect_error("anon cannot select " + t, "permission denied", lambda t=t: select(None, t))
expect_error("authenticated cannot select blocked_terms", "permission denied", lambda: select(JWT_A, "blocked_terms"))
expect_error("authenticated cannot select session_participants", "permission denied", lambda: select(JWT_A, "session_participants"))

# ---------- 2. start a session ----------
expect_error("start_session rejects invalid questions", "invalid questions", lambda: rpc(JWT_A, "start_session", p_deck_key="Deck", p_polls=[{"slide_key": "x", "questions": [{"type": "choice", "prompt": "x", "options": ["a"]}]}]))
expect_error("start_session rejects a poll without slide_key", "slide_key", lambda: rpc(JWT_A, "start_session", p_deck_key="Deck", p_polls=[{"questions": Q_CHOICE}]))
sess = rpc(JWT_A, "start_session", p_deck_key="REST test " + time.strftime("%H:%M:%S"), p_polls=POLLS)[0]
SID, CODE = sess["session_id"], sess["code"]
check("start_session returns id, 6-char code, expiry, no secret", len(CODE) == 6 and "expires_at" in sess and set(sess) == {"session_id", "code", "expires_at"}, sess)
st = rpc(JWT_A, "session_state", p_session=SID)
check("owner state is waiting with 4 drafts in order", st["state"] == "waiting" and st["is_owner"] and [p["slide_key"] for p in st["polls"]] == ["misconceptions", "jagged", "flow", "preface"] and all(p["status"] == "draft" for p in st["polls"]), st)
check("server_time present", "server_time" in st)
polls = {p["slide_key"]: p["id"] for p in st["polls"]}
A, B, C, D = polls["misconceptions"], polls["jagged"], polls["flow"], polls["preface"]
check("owner reads own poll rows through RLS", len(select(JWT_A, "session_polls", "select=id&session_id=eq." + SID)) == 4)
check("owner B reads no poll rows", select(JWT_B, "session_polls", "select=id") == [])
check("owner B reads no session rows", select(JWT_B, "poll_sessions", "select=id") == [])
expect_error("owner B cannot open A's poll", "Not allowed", lambda: rpc(JWT_B, "open_poll", p_poll=A))
check("owner B gets ended for A's session", rpc(JWT_B, "session_state", p_session=SID)["state"] == "ended")
expect_error("no role can update sessions", "permission denied", lambda: write(JWT_A, "PATCH", "poll_sessions", {"max_participants": 5}, "id=eq." + SID))
expect_error("no role can insert polls", "permission denied", lambda: write(JWT_A, "POST", "session_polls", {"session_id": SID, "slide_key": "x", "position": 9, "questions": Q_CHOICE}))

# ---------- 3. phones join ----------
j = rpc(None, "join_session", p_code=CODE, p_token=P1)[0]
check("phone 1 joins by code", j["session_id"] == SID)
j = rpc(None, "join_session", p_code=CODE.lower(), p_token=P1)[0]
check("rejoin is idempotent and case-insensitive", j["session_id"] == SID)
expect_error("bad token (63 chars)", "Invalid device token", lambda: rpc(None, "join_session", p_code=CODE, p_token=P1[:63]))
expect_error("unknown code", "not found", lambda: rpc(None, "join_session", p_code="ZZZZZZ", p_token=P2))
st = rpc(None, "session_state", p_session=SID, p_token=P1)
check("phone 1 sees waiting", st["state"] == "waiting" and st["poll"] is None and st["polls"] is None and st["is_owner"] is False, st)
check("outsider token gets ended", rpc(None, "session_state", p_session=SID, p_token=OUT)["state"] == "ended")
check("no token gets ended", rpc(None, "session_state", p_session=SID)["state"] == "ended")
expect_error("phone cannot read results of a draft", "Not allowed", lambda: rpc(None, "poll_results", p_poll=A, p_token=P1))
expect_error("phone cannot vote on a draft", "not open", lambda: rpc(None, "save_answers", p_poll=A, p_answers={"0": {"choice": 0}}, p_token=P1))

# ---------- 4. launch A with a Realtime listener ----------
bc = run_with_listener(CODE, lambda: rpc(JWT_A, "open_poll", p_poll=A))
check("phone websocket joins the public topic without sign-in", bc.get("joined") is True, bc.get("join_reply"))
msgs = bc.get("messages", [])
check("launch broadcasts one poll-changed hint", len(msgs) == 1 and msgs[0].get("event") == "poll-changed" and msgs[0].get("payload", {}).get("status") == "open" and msgs[0]["payload"].get("poll_id") == A, msgs)
st = rpc(None, "session_state", p_session=SID, p_token=P1)
check("phone 1 sees A open with 3 questions", st["state"] == "open" and st["poll"]["id"] == A and len(st["poll"]["questions"]) == 3, st)
r = rpc(None, "save_answers", p_poll=A, p_answers={"0": {"choice": 0}}, p_token=P1)
check("single answer saves", r["saved"] == 1, r)
r = rpc(None, "save_answers", p_poll=A, p_answers={"0": {"choice": 2}, "1": {"choice": 1}, "2": {"choice": 0}}, p_token=P1)
check("batch of 3 saves", r["saved"] == 3, r)
res = rpc(JWT_A, "poll_results", p_poll=A)
check("owner sees counts; respondent count stays 1 after a change", res["respondents"] == 1 and res["questions"][0]["counts"] == [0, 0, 1] and res["questions"][1]["counts"] == [0, 1, 0], res)
expect_error("batch with one bad answer is rejected whole", "Bad answer", lambda: rpc(None, "save_answers", p_poll=A, p_answers={"0": {"choice": 1}, "1": {"choice": 7}}, p_token=P1))
check("rejected batch changed nothing", rpc(JWT_A, "poll_results", p_poll=A)["questions"][0]["counts"] == [0, 0, 1])
expect_error("outsider cannot vote", "not open", lambda: rpc(None, "save_answers", p_poll=A, p_answers={"0": {"choice": 1}}, p_token=OUT))
expect_error("phone cannot read results while open", "Not allowed", lambda: rpc(None, "poll_results", p_poll=A, p_token=P1))
check("owner's live rows via RLS: 3 answers", len(select(JWT_A, "session_answers", "select=question_index&poll_id=eq." + A)) == 3)
check("owner B sees no answers via RLS", select(JWT_B, "session_answers", "select=question_index&poll_id=eq." + A) == [])
st = rpc(None, "session_state", p_session=SID, p_token=P1)
check("phone 1 gets its own answers back", st["mine"]["answers"] == {"0": {"choice": 2}, "1": {"choice": 1}, "2": {"choice": 0}}, st["mine"])

# ---------- 5. close, reveal one at a time ----------
bc = run_with_listener(CODE, lambda: (rpc(JWT_A, "close_poll", p_poll=A), rpc(JWT_A, "reveal_questions", p_poll=A, p_revealed=[0])))
seq = [(m["payload"]["status"], m["payload"]["revealed"]) for m in bc.get("messages", [])]
check("close and reveal each broadcast a hint", seq == [("closed", []), ("closed", [0])], seq)
expect_error("vote after close is rejected", "not open", lambda: rpc(None, "save_answers", p_poll=A, p_answers={"0": {"choice": 1}}, p_token=P1))
st = rpc(None, "session_state", p_session=SID, p_token=P1)
check("phone 1 sees closed with revealed [0]", st["state"] == "closed" and st["poll"]["revealed"] == [0], st)
res = rpc(None, "poll_results", p_poll=A, p_token=P1)
check("phone sees only question 1", res["questions"][0]["counts"] == [0, 0, 1] and "counts" not in res["questions"][1] and res["questions"][1]["revealed"] is False, res)
rpc(JWT_A, "reveal_questions", p_poll=A, p_revealed=[0, 1, 2])
res = rpc(None, "poll_results", p_poll=A, p_token=P1)
check("phone sees all 3 after full reveal", all("counts" in q for q in res["questions"]), res)
expect_error("outsider cannot read closed results", "Not allowed", lambda: rpc(None, "poll_results", p_poll=A, p_token=OUT))
rpc(JWT_A, "open_poll", p_poll=A)
st = rpc(None, "session_state", p_session=SID, p_token=P1)
check("reopen keeps answers and clears reveal", st["state"] == "open" and st["mine"]["answers"]["0"] == {"choice": 2} and st["poll"]["revealed"] == [], st)

# ---------- 6. launch B while A open; late joiner; ranking ----------
rpc(JWT_A, "open_poll", p_poll=B)
statuses = {p["slide_key"]: p["status"] for p in rpc(JWT_A, "session_state", p_session=SID)["polls"]}
check("launching B closes A first", statuses == {"misconceptions": "closed", "jagged": "open", "flow": "draft", "preface": "draft"}, statuses)
rpc(None, "join_session", p_code=CODE, p_token=P2)
st = rpc(None, "session_state", p_session=SID, p_token=P2)
check("late joiner lands on open poll B", st["state"] == "open" and st["poll"]["id"] == B, st)
expect_error("ranking must be a full permutation", "Bad answer", lambda: rpc(None, "save_answers", p_poll=B, p_answers={"0": {"order": [0, 1, 2, 3, 3]}}, p_token=P1))
rpc(None, "save_answers", p_poll=B, p_answers={"0": {"order": [4, 3, 2, 1, 0]}}, p_token=P1)
rpc(None, "save_answers", p_poll=B, p_answers={"0": {"order": [0, 4, 3, 2, 1]}}, p_token=P2)
rpc(JWT_A, "close_poll", p_poll=B)
rpc(JWT_A, "reveal_questions", p_poll=B, p_revealed=[0])
res = rpc(None, "poll_results", p_poll=B, p_token=P2)
check("average ranks match a hand count", [float(x) for x in res["questions"][0]["average_rank"]] == [3.0, 4.5, 3.5, 2.5, 1.5] and res["respondents"] == 2, res)

# ---------- 7. rating ----------
rpc(JWT_A, "open_poll", p_poll=C)
expect_error("rating above max", "Bad answer", lambda: rpc(None, "save_answers", p_poll=C, p_answers={"0": {"value": 4}}, p_token=P1))
rpc(None, "save_answers", p_poll=C, p_answers={"0": {"value": 1}, "1": {"value": 3}, "2": {"value": 2}, "3": {"value": 3}}, p_token=P1)
rpc(None, "save_answers", p_poll=C, p_answers={"0": {"value": 3}, "1": {"value": 3}}, p_token=P2)
rpc(JWT_A, "close_poll", p_poll=C)
res = rpc(JWT_A, "poll_results", p_poll=C)
check("rating counts and mean", res["questions"][0]["counts"] == [1, 0, 1] and float(res["questions"][0]["mean"]) == 2.0 and res["questions"][1]["counts"] == [0, 0, 2], res)

# ---------- 8. free text ----------
rpc(JWT_A, "open_poll", p_poll=D)
e1 = rpc(None, "add_text_entry", p_poll=D, p_q=0, p_body="  What the AI drafted and what I changed  ", p_token=P1)
e2 = rpc(None, "add_text_entry", p_poll=D, p_q=0, p_body="This is fucking useless", p_token=P1)
e3 = rpc(None, "add_text_entry", p_poll=D, p_q=0, p_body="Which parts to check first", p_token=P2)
e4 = rpc(None, "add_text_entry", p_poll=D, p_q=0, p_body="Shitty draft, sorry", p_token=P2)
expect_error("empty entry rejected", "1 to 280", lambda: rpc(None, "add_text_entry", p_poll=D, p_q=0, p_body="   ", p_token=P2))
rpc(None, "add_text_entry", p_poll=D, p_q=0, p_body="third", p_token=P1)
expect_error("entry limit holds", "Entry limit", lambda: rpc(None, "add_text_entry", p_poll=D, p_q=0, p_body="fourth", p_token=P1))
res = rpc(JWT_A, "poll_results", p_poll=D)
ents = {e["id"]: e for e in res["questions"][0]["entries"]}
check("word filter masks whole words only", ents[e2]["masked"] is True and ents[e1]["masked"] is False and ents[e4]["masked"] is False, ents)
check("entry body trimmed", ents[e1]["body"] == "What the AI drafted and what I changed")
expect_error("phone 2 cannot remove phone 1's entry", "Not allowed", lambda: rpc(None, "remove_text_entry", p_entry=e1, p_token=P2))
rpc(None, "remove_text_entry", p_entry=e1, p_token=P1)
rpc(JWT_A, "hide_text_entry", p_entry=e3, p_hidden=True)
expect_error("owner B cannot hide A's entry", "Not allowed", lambda: rpc(JWT_B, "hide_text_entry", p_entry=e4, p_hidden=True))
rpc(JWT_A, "close_poll", p_poll=D)
expect_error("remove after close rejected", "not open", lambda: rpc(None, "remove_text_entry", p_entry=e2, p_token=P1))
rpc(JWT_A, "reveal_questions", p_poll=D, p_revealed=[0])
res = rpc(None, "poll_results", p_poll=D, p_token=P2)
bodies = [e["body"] for e in res["questions"][0]["entries"]]
check("phones see only clean, unhidden entries", bodies == ["Shitty draft, sorry", "third"] and all("id" not in e for e in res["questions"][0]["entries"]), bodies)
check("owner live rows via RLS: 4 entries", len(select(JWT_A, "session_text_entries", "select=id&poll_id=eq." + D)) == 4)

# ---------- 9. a second session for the same owner is independent ----------
sess2 = rpc(JWT_A, "start_session", p_deck_key="REST test 2", p_polls=[POLLS[0]])[0]
check("second session gets a different code", sess2["code"] != CODE)
expect_error("phone 1 (joined session 1 only) gets nothing from session 2", "not open", lambda: rpc(None, "save_answers", p_poll=rpc(JWT_A, "session_state", p_session=sess2["session_id"])["polls"][0]["id"], p_answers={"0": {"choice": 0}}, p_token=P1))
check("phone 1 sees session 2 as ended (not joined)", rpc(None, "session_state", p_session=sess2["session_id"], p_token=P1)["state"] == "ended")

n_fail = sum(1 for _, ok in results if not ok)
print("\n%d checks, %d failed" % (len(results), n_fail))
print("session code for manual checks:", CODE, "session id:", SID)
sys.exit(1 if n_fail else 0)
