#!/usr/bin/env python3
"""Drives the respondent session app end to end with Playwright against the fake Supabase edge
(real SQL in local PostgreSQL). Phone 1 = mobile viewport, phone 2 = laptop viewport.
The facilitator is a plain HTTP client (as the deck will be).

  python3 test_respondent_page.py  (expects fake_supabase.py on 8787 serving test/index.local.html)
"""
import json, sys, time, uuid, pathlib, requests
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8787"; KEY = "local-anon-key"
SHOTS = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parent / "shots"); SHOTS.mkdir(parents=True, exist_ok=True)
results = []
def check(name, ok, detail=""):
    results.append((name, ok)); print(("PASS " if ok else "FAIL ") + name + (("  -- " + str(detail)[:300]) if detail and not ok else ""), flush=True)

def H(jwt=None): return {"apikey": KEY, "Authorization": "Bearer " + (jwt or KEY), "Content-Type": "application/json"}
def rpc(jwt, fn, **p):
    r = requests.post(BASE + "/rest/v1/rpc/" + fn, headers=H(jwt), data=json.dumps(p));
    if r.status_code >= 400: raise RuntimeError("%s %s" % (r.status_code, r.text))
    return r.json() if r.text else None
JWT = requests.post(BASE + "/auth/v1/signup", headers={"apikey": KEY}).json()["access_token"]

Q_CHOICE = [{"type": "choice", "prompt": "AI performs predictably across similar tasks.", "options": ["Agree", "Unsure", "Disagree"]},
            {"type": "choice", "prompt": "When AI increases performance, it also improves skill.", "options": ["Agree", "Unsure", "Disagree"]},
            {"type": "choice", "prompt": "AI productivity is the time saved generating an output.", "options": ["Agree", "Unsure", "Disagree"]}]
Q_RANK = [{"type": "ranking", "prompt": "Rank these tasks from hardest to easiest for AI.", "rankLabel": "hardest",
           "options": ["Count the r’s in strawberry", "Reply to an email", "Summarize text", "Generate code", "Write an essay"]}]
Q_RATE = [{"type": "rating", "prompt": "Frontier: how much more evidence do I need?", "min": 1, "max": 3, "labels": {"min": "Low", "max": "High"}},
          {"type": "rating", "prompt": "Learning: how important is practicing the skill?", "min": 1, "max": 3, "labels": {"min": "Low", "max": "High"}}]
Q_TEXT = [{"type": "text", "prompt": "What could you include when sharing AI-generated work?", "maxEntries": 3}]
POLLS = [{"slide_key": "misconceptions", "questions": Q_CHOICE}, {"slide_key": "jagged", "questions": Q_RANK},
         {"slide_key": "flow", "questions": Q_RATE}, {"slide_key": "preface", "questions": Q_TEXT}]
sess = rpc(JWT, "start_session", p_deck_key="Playwright", p_polls=POLLS)[0]
SID, CODE = sess["session_id"], sess["code"]
polls = {p["slide_key"]: p["id"] for p in rpc(JWT, "session_state", p_session=SID)["polls"]}
A, B, C, D = polls["misconceptions"], polls["jagged"], polls["flow"], polls["preface"]
print("session", CODE)

def wait_text(page, text, timeout=8000):
    try:
        page.get_by_text(text, exact=False).first.wait_for(timeout=timeout); return True
    except Exception:
        return False

with sync_playwright() as pw:
    browser = pw.chromium.launch()
    phone = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True)
    laptop = browser.new_context(viewport={"width": 1280, "height": 800}, device_scale_factor=1)
    p1 = phone.new_page(); p2 = laptop.new_page()
    errors = []
    p1.on("pageerror", lambda e: errors.append("p1: " + str(e))); p2.on("pageerror", lambda e: errors.append("p2: " + str(e)))
    p1.on("console", lambda m: errors.append("p1 console " + m.type + ": " + m.text) if m.type == "error" and "Failed to load resource" not in m.text else None)

    # bare URL -> join form
    p1.goto(BASE + "/"); check("bare URL shows the join form", wait_text(p1, "Join a live poll"))
    p1.screenshot(path=str(SHOTS / "01-join-phone.png"))
    p1.fill("#s-codein", CODE.lower()); p1.click("button:has-text('Join')")
    p1.wait_for_url(lambda u: u.endswith("?s=" + CODE), timeout=5000)
    check("join form navigates to ?s=CODE (uppercased)", p1.url.endswith("?s=" + CODE), p1.url)
    check("phone 1 sees Waiting", wait_text(p1, "You’re in."))
    check("phone 1 shows the session code", CODE in p1.inner_text("#s-code"))
    p1.screenshot(path=str(SHOTS / "02-waiting-phone.png"))
    tok1 = p1.evaluate("localStorage.getItem('llpoll-device')")
    check("device token stored (64 hex)", bool(tok1) and len(tok1) == 64)

    # launch A: phone follows within 2 s without reload
    rpc(JWT, "open_poll", p_poll=A); t0 = time.time()
    check("phone 1 switches to A on broadcast", wait_text(p1, "AI performs predictably", 2500)); dt = time.time() - t0
    check("...within 2 seconds (%.2fs)" % dt, dt < 2.0)
    p1.screenshot(path=str(SHOTS / "03-open-choice-phone.png"), full_page=True)
    check("status line is empty before the first tap (no stray text)", p1.inner_text("#s-status").strip() == "", p1.inner_text("#s-status"))
    # three quick taps -> one save with 3 answers
    p1.click("[data-q='0'] .s-opt >> nth=0"); p1.click("[data-q='1'] .s-opt >> nth=1"); p1.click("[data-q='2'] .s-opt >> nth=2")
    check("Saved appears", wait_text(p1, "Saved", 4000))
    time.sleep(0.6)
    res = rpc(JWT, "poll_results", p_poll=A)
    check("three quick taps stored as one respondent with 3 answers", res["respondents"] == 1 and res["questions"][0]["counts"] == [1, 0, 0] and res["questions"][1]["counts"] == [0, 1, 0] and res["questions"][2]["counts"] == [0, 0, 1], res)
    check("screen-reader live region announced Saved", "Saved" in p1.inner_text("#s-live"))
    p1.click("[data-q='0'] .s-opt >> nth=2"); time.sleep(1.2)
    res = rpc(JWT, "poll_results", p_poll=A)
    check("changing an answer keeps the count at 1", res["respondents"] == 1 and res["questions"][0]["counts"] == [0, 0, 1], res)
    p1.screenshot(path=str(SHOTS / "04-choice-saved-phone.png"), full_page=True)
    # reload restores answers
    p1.reload(); check("reload restores the open poll", wait_text(p1, "AI performs predictably"))
    check("reload restores selected answers", p1.get_attribute("[data-q='0'] .s-opt >> nth=2", "aria-checked") == "true" and p1.get_attribute("[data-q='2'] .s-opt >> nth=2", "aria-checked") == "true")

    # close A -> Look up, tap rejected
    rpc(JWT, "close_poll", p_poll=A); t0 = time.time()
    check("phone 1 shows Look up on close", wait_text(p1, "Look up at the screen", 2500)); check("...within 2 seconds (%.2fs)" % (time.time() - t0), time.time() - t0 < 2.0)
    check("own answers shown under Closed", wait_text(p1, "Your answers") and "Disagree" in p1.inner_text("#s-view"))
    p1.screenshot(path=str(SHOTS / "05-closed-phone.png"), full_page=True)
    # laptop joins while A is closed and unrevealed, with no answers of its own: Look up, no stray "null"
    p2.goto(BASE + "/?s=" + CODE); check("laptop joining a closed poll sees Look up", wait_text(p2, "Look up at the screen"))
    check("closed view without own answers has no stray text", "null" not in p2.inner_text("#s-view") and "Your answers" not in p2.inner_text("#s-view"), p2.inner_text("#s-view"))
    # reveal one at a time
    rpc(JWT, "reveal_questions", p_poll=A, p_revealed=[0])
    check("reveal 1 shows question 1 results", wait_text(p1, "100% (1)", 3000))
    txt = p1.inner_text("#s-view")
    check("questions 2 and 3 still say look up", txt.count("Results for this question appear") == 2, txt[:400])
    check("own answer is marked", "Your answer" in txt)
    rpc(JWT, "reveal_questions", p_poll=A, p_revealed=[0, 1, 2]); time.sleep(1.5)
    txt = p1.inner_text("#s-view"); check("full reveal shows all 3", txt.count("Results for this question appear") == 0 and txt.count("Your answer") == 3, txt[:300])
    p1.screenshot(path=str(SHOTS / "06-results-choice-phone.png"), full_page=True)
    p1.reload(); check("reload shows revealed results", wait_text(p1, "Results") and wait_text(p1, "100% (1)"))

    # phone 2 (laptop) joins during Waiting between A and B? A is closed and revealed, so it sees results of A; the plan wants Waiting between polls.
    # Simulate "between polls": reveal cleared and A stays closed -> phone shows Look up. A true Waiting only happens before the first poll.
    p2.reload(); check("laptop reload lands in the current state (A's revealed results)", wait_text(p2, "Results"))
    p2.screenshot(path=str(SHOTS / "07-results-laptop.png"), full_page=True)

    # launch B (ranking)
    rpc(JWT, "open_poll", p_poll=B)
    check("both switch to B", wait_text(p1, "Rank these tasks", 2500) and wait_text(p2, "Rank these tasks", 2500))
    time.sleep(1.0)
    res = rpc(JWT, "poll_results", p_poll=B); check("untouched default order does not count", res["respondents"] == 0, res)
    p1.screenshot(path=str(SHOTS / "08-ranking-phone.png"), full_page=True)
    p1.click("[data-q='0'] .s-rank li >> nth=4 >> button >> nth=0")  # move last item up
    check("ranking saves after the first move", wait_text(p1, "Saved", 3000)); time.sleep(0.8)
    check("rank hint after a move has no stray text", "Your order is saved" in p1.inner_text("#s-view") and "null" not in p1.inner_text("#s-view"), p1.inner_text("#s-view")[:400])
    res = rpc(JWT, "poll_results", p_poll=B); check("phone 1 counted after a move", res["respondents"] == 1, res)
    p2.click("text=This order is right"); check("laptop saves with This order is right", wait_text(p2, "Saved", 3000)); time.sleep(0.8)
    res = rpc(JWT, "poll_results", p_poll=B); check("both counted", res["respondents"] == 2, res)
    rpc(JWT, "close_poll", p_poll=B); rpc(JWT, "reveal_questions", p_poll=B, p_revealed=[0])
    check("ranking results shown", wait_text(p1, "avg ", 3000))
    res = rpc(JWT, "poll_results", p_poll=B)
    # hand count: p1 order [0,1,2,4,3] (moved item 4 up one), p2 [0,1,2,3,4]: option3 avg (5+4)/2=4.5, option4 avg (4+5)/2=4.5
    check("average ranks match hand count", [float(x) for x in res["questions"][0]["average_rank"]] == [1.0, 2.0, 3.0, 4.5, 4.5], res["questions"][0])
    p1.screenshot(path=str(SHOTS / "09-ranking-results-phone.png"), full_page=True)

    # launch C (rating)
    rpc(JWT, "open_poll", p_poll=C)
    check("both switch to C", wait_text(p1, "Frontier", 2500) and wait_text(p2, "Frontier", 2500))
    p1.click("[data-q='0'] .s-scale button >> nth=0"); p1.click("[data-q='1'] .s-scale button >> nth=2")
    p2.click("[data-q='0'] .s-scale button >> nth=2")
    check("rating saved", wait_text(p1, "Saved", 3000) and wait_text(p2, "Saved", 3000)); time.sleep(0.8)
    p1.screenshot(path=str(SHOTS / "10-rating-phone.png"), full_page=True)
    rpc(JWT, "close_poll", p_poll=C); rpc(JWT, "reveal_questions", p_poll=C, p_revealed=[0, 1])
    check("rating results show average", wait_text(p1, "Average 2.0 of 3", 3000))
    p1.screenshot(path=str(SHOTS / "11-rating-results-phone.png"), full_page=True)

    # launch D (text)
    rpc(JWT, "open_poll", p_poll=D)
    check("both switch to D", wait_text(p1, "What could you include", 2500) and wait_text(p2, "What could you include", 2500))
    p1.fill("[data-q='0'] textarea", "What the AI drafted and what I changed"); p1.click("[data-q='0'] >> text=Add")
    check("entry added and Saved", wait_text(p1, "Saved", 3000) and wait_text(p1, "What the AI drafted and what I changed"))
    p1.fill("[data-q='0'] textarea", "This is fucking useless"); p1.click("[data-q='0'] >> text=Add"); time.sleep(0.8)
    p2.fill("[data-q='0'] textarea", "Which parts to check first"); p2.click("[data-q='0'] >> text=Add"); time.sleep(0.8)
    p2.fill("[data-q='0'] textarea", "Deadline context"); p2.click("[data-q='0'] >> text=Add"); time.sleep(0.8)
    check("entry counter", "2 of 3 entries" in p1.inner_text("[data-q='0'] .s-count"))
    p1.screenshot(path=str(SHOTS / "12-text-phone.png"), full_page=True)
    p1.click("[data-q='0'] .s-entries li >> nth=1 >> text=Remove"); time.sleep(0.8)
    check("remove works", "1 of 3 entries" in p1.inner_text("[data-q='0'] .s-count"))
    p1.fill("[data-q='0'] textarea", "This is fucking useless"); p1.click("[data-q='0'] >> text=Add"); time.sleep(0.8)
    time.sleep(0.8)
    check("own masked entry shows a Filtered note", "Filtered" in p1.inner_text("[data-q='0'] .s-entries"))
    res = rpc(JWT, "poll_results", p_poll=D)
    ents = res["questions"][0]["entries"]
    masked = [e for e in ents if e["masked"]]
    check("blocked word masked on the deck side", len(masked) == 1 and masked[0]["body"] == "This is fucking useless", ents)
    hide_id = [e["id"] for e in ents if e["body"] == "Deadline context"][0]
    rpc(JWT, "hide_text_entry", p_entry=hide_id, p_hidden=True)
    rpc(JWT, "close_poll", p_poll=D); rpc(JWT, "reveal_questions", p_poll=D, p_revealed=[0])
    check("text results shown", wait_text(p1, "Which parts to check first", 3000) and wait_text(p2, "Which parts to check first", 3000))
    t1, t2 = p1.inner_text("#s-view"), p2.inner_text("#s-view")
    check("masked entry never appears on phones", "fucking" not in t1 and "fucking" not in t2)
    check("hidden entry gone from both phones", "Deadline context" not in t1 and "Deadline context" not in t2)
    check("own entry marked Yours", "Yours" in t1)
    p1.screenshot(path=str(SHOTS / "13-text-results-phone.png"), full_page=True)
    p2.screenshot(path=str(SHOTS / "14-text-results-laptop.png"), full_page=True)

    # reopen keeps answers: reopen A and check selected states
    rpc(JWT, "open_poll", p_poll=A)
    check("reopen A shows voting again", wait_text(p1, "AI performs predictably", 2500))
    check("reopened poll shows earlier answers", p1.get_attribute("[data-q='0'] .s-opt >> nth=2", "aria-checked") == "true")
    rpc(JWT, "close_poll", p_poll=A)

    # vote after close is refused with a message: open C, select, then close before the batch flushes
    rpc(JWT, "open_poll", p_poll=C); wait_text(p1, "Frontier", 2500)
    p1.click("[data-q='0'] .s-scale button >> nth=1"); rpc(JWT, "close_poll", p_poll=C)
    check("late change shows the closed-before-saved message or Look up", wait_text(p1, "closed before your last change", 3000) or wait_text(p1, "Look up", 2000))

    # expiry: set the clock back (admin), phones go to Ended on the next refetch
    import psycopg2
    adm = psycopg2.connect("dbname=llpoll user=postgres password=postgres host=127.0.0.1"); adm.autocommit = True
    cur = adm.cursor(); cur.execute("alter table public.poll_sessions disable trigger all")
    cur.execute("update public.poll_sessions set created_at = created_at - interval '2 hours', expires_at = expires_at - interval '2 hours' where id = %s", (SID,))
    cur.execute("alter table public.poll_sessions enable trigger all")
    p1.reload(); check("expired session shows Ended on reload", wait_text(p1, "This session has ended", 6000))
    p1.screenshot(path=str(SHOTS / "15-ended-phone.png"))
    check("laptop shows Ended within the heartbeat (30 s)", wait_text(p2, "This session has ended", 35000))
    time.sleep(0.5)
    check("Reconnecting banner stays hidden on an ended session", p1.is_hidden("#s-banner") and p2.is_hidden("#s-banner"))

    # unknown code
    p1.goto(BASE + "/?s=ZZZZZZ"); check("unknown code shows ended with a check-the-code line", wait_text(p1, "Check the code with the facilitator"))
    check("no page errors", not errors, errors[:5])
    browser.close()

n_fail = sum(1 for _, ok in results if not ok)
print("\n%d checks, %d failed" % (len(results), n_fail)); sys.exit(1 if n_fail else 0)
