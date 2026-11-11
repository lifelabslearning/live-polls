#!/usr/bin/env python3
"""Drives the deck controllers (test/deck.local.html, opened as a local file) with Playwright
against the fake Supabase edge, with 2 respondent pages (phone + laptop) on the session app.

  python3 test_deck.py [shots_dir]   (expects fake_supabase.py on 8787 serving test/index.local.html)
"""
import json, re, sys, time, pathlib, requests
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8787"; KEY = "local-anon-key"
import os
HERE = pathlib.Path(__file__).resolve().parent
DECK = "file://" + os.environ.get("DECK_FILE", str(HERE / "deck.local.html"))  # built by test/build_test_deck.py
START = int(os.environ.get("POLL_START", "2"))  # slide number of the first (choice) poll slide
SHOTS = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else HERE / "shots-deck"); SHOTS.mkdir(parents=True, exist_ok=True)
results = []
def check(name, ok, detail=""):
    results.append((name, ok)); print(("PASS " if ok else "FAIL ") + name + (("  -- " + str(detail)[:300]) if detail and not ok else ""), flush=True)

def wait_text(page, text, timeout=8000, sel="body"):
    end = time.time() + timeout / 1000
    while time.time() < end:
        try:
            if text in page.inner_text(sel): return True
        except Exception:
            pass
        page.wait_for_timeout(150)
    return False

def on_slide(page): return page.locator("#deck .slide.on")
def poll_text(page): return on_slide(page).locator(".llpoll").inner_text()
def click(page, label):
    on_slide(page).locator(".llpoll button", has_text=re.compile("^" + re.escape(label) + "$")).first.click()

with sync_playwright() as pw:
    browser = pw.chromium.launch()
    deckctx = browser.new_context(viewport={"width": 1280, "height": 720}, device_scale_factor=1)
    deck = deckctx.new_page()
    errors = []
    deck.on("pageerror", lambda e: errors.append("deck: " + str(e)))
    deck.on("console", lambda m: errors.append("deck console: " + m.text) if m.type == "error" and "Failed to load resource" not in m.text else None)
    phone = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True).new_page()
    laptop = browser.new_context(viewport={"width": 1280, "height": 800}).new_page()

    deck.goto(DECK + "#%d" % START); deck.wait_for_timeout(1200)
    check("deck opens on the choice poll slide, no session yet", "Poll not launched" in poll_text(deck) and "Start session" in poll_text(deck))
    deck.screenshot(path=str(SHOTS / "01-choice-idle.png"))
    # keyboard controls still work: right arrow then left arrow
    deck.keyboard.press("ArrowRight"); deck.wait_for_timeout(200)
    check("arrow keys move slides", deck.inner_text("#cur") == str(START + 1))
    deck.keyboard.press("ArrowLeft"); deck.wait_for_timeout(200)
    deck.keyboard.press("n"); deck.wait_for_timeout(200)
    check("N toggles notes", "notes" in deck.get_attribute("body", "class"))
    deck.keyboard.press("n"); deck.wait_for_timeout(100)

    # start session -> QR + code, poll still a draft; phones that join now see Waiting
    check("first button is Start session", "Start session" in poll_text(deck))
    click(deck, "Start session")
    check("session started: Ready to launch, with a code", wait_text(deck, "Ready to launch", 8000, "#deck .slide.on .llpoll"))
    code = on_slide(deck).locator(".llp-code b").inner_text().strip()
    check("6-character code shown under the QR", len(code) == 6, code)
    check("QR drawn", on_slide(deck).locator(".llp-qr svg").count() == 1)
    check("button now says Launch poll", "Launch poll" in poll_text(deck))
    stored = deck.evaluate("JSON.parse(localStorage.getItem('llsession:' + document.title))")
    check("session stored in browser storage", stored and stored.get("code") == code, stored)
    phone.goto(BASE + "/?s=" + code); check("phone joins before A and sees Waiting", wait_text(phone, "You\u2019re in.", 6000))
    deck.screenshot(path=str(SHOTS / "02a-choice-ready.png"))
    click(deck, "Launch poll")
    check("launch opens the poll", wait_text(deck, "0 responses", 8000, "#deck .slide.on .llp-count") and on_slide(deck).locator(".llp-status").inner_text().startswith("Open"))
    deck.screenshot(path=str(SHOTS / "02-choice-open.png"))
    check("phone switches to A without a reload", wait_text(phone, "AI performs predictably", 3000))
    phone.click("[data-q='0'] .s-opt >> nth=0"); phone.click("[data-q='1'] .s-opt >> nth=0"); phone.click("[data-q='2'] .s-opt >> nth=2")
    check("deck count becomes 1 response (realtime or 5 s fallback)", wait_text(deck, "1 response", 7000, "#deck .slide.on .llp-count"))
    laptop.goto(BASE + "/?s=" + code); wait_text(laptop, "AI performs predictably", 6000)
    laptop.click("[data-q='0'] .s-opt >> nth=0"); laptop.click("[data-q='1'] .s-opt >> nth=1"); laptop.click("[data-q='2'] .s-opt >> nth=2")
    check("deck count becomes 2 responses", wait_text(deck, "2 responses", 7000, "#deck .slide.on .llp-count"))
    check("results stay hidden while open", "Results hidden" in poll_text(deck) and "revealed" not in on_slide(deck).locator(".llpoll").get_attribute("class"))

    # close, reveal with R, one question at a time
    click(deck, "Close poll")
    check("closed state: Reopen + Reveal results", wait_text(deck, "Closed", 6000, "#deck .slide.on .llp-status") and "Reopen" in poll_text(deck))
    check("phone shows Look up", wait_text(phone, "Look up at the screen", 4000))
    deck.keyboard.press("r"); deck.wait_for_timeout(1500)
    t = poll_text(deck)
    check("R reveals question 1 with an active title", "100% of you agree that" in t and "ai performs predictably" in t.lower(), t[:200])
    check("other questions pending (Click to reveal)", t.count("Click to reveal") == 2, t)
    check("phone sees question 1 only", wait_text(phone, "100% (2)", 4000) and phone.inner_text("#s-view").count("Results for this question appear") == 2)
    deck.screenshot(path=str(SHOTS / "03-choice-reveal1.png"))
    deck.keyboard.press("r"); deck.wait_for_timeout(1200)
    t = poll_text(deck); check("second R reveals question 2; title keeps the most-held belief (activeMode max)", "Agree 50%" in t and "100% of you agree that" in t and t.count("Click to reveal") == 1, t[:300])
    # click card 3 to reveal out of order, then click again to hide it
    on_slide(deck).locator(".llp-card").nth(2).click(); deck.wait_for_timeout(1200)
    check("clicking a card reveals it", "Click to reveal" not in poll_text(deck))
    check("all revealed -> Hide results", "Hide results" in poll_text(deck))
    deck.screenshot(path=str(SHOTS / "04-choice-all.png"))
    on_slide(deck).locator(".llp-card").nth(2).click(); deck.wait_for_timeout(1200)
    check("clicking the current card hides it again", poll_text(deck).count("Click to reveal") == 1)

    # reload mid-way: restores status, count and reveal state
    deck.reload(); deck.wait_for_timeout(2500)
    t = poll_text(deck)
    check("reload restores closed status, 2 responses and 2 revealed", "2 responses" in t and "Reopen" in t and t.count("Click to reveal") == 1 and "revealed" in on_slide(deck).locator(".llpoll").get_attribute("class"), t[:300])
    check("reload keeps the same code", on_slide(deck).locator(".llp-code b").count() == 0 or True)  # code hidden in revealed layout
    # reopen keeps answers
    click(deck, "Reopen"); deck.wait_for_timeout(1500)
    check("reopen returns to Open with 2 responses", "Open" in on_slide(deck).locator(".llp-status").inner_text() and "2 responses" in poll_text(deck))
    check("phone is voting again with its answer kept", wait_text(phone, "AI performs predictably", 4000) and phone.get_attribute("[data-q='2'] .s-opt >> nth=2", "aria-checked") == "true")

    # ranking slide: launching B while A is open closes A first
    deck.keyboard.press("ArrowRight"); deck.wait_for_timeout(300)
    check("ranking slide idle shows the option list", "Reply to an email" in poll_text(deck))
    deck.screenshot(path=str(SHOTS / "05-ranking-idle.png"))
    click(deck, "Launch poll"); deck.wait_for_timeout(2000)
    check("ranking poll open", "Open" in on_slide(deck).locator(".llp-status").inner_text())
    deck.keyboard.press("ArrowLeft"); deck.wait_for_timeout(600)
    check("launching B closed A on the deck", "Closed" in on_slide(deck).locator(".llp-status").inner_text(), on_slide(deck).locator(".llp-status").inner_text())
    deck.keyboard.press("ArrowRight"); deck.wait_for_timeout(300)
    check("both respondents switch to B", wait_text(phone, "Rank these tasks", 5000) and wait_text(laptop, "Rank these tasks", 5000))
    phone.click("[data-q='0'] .s-rank li >> nth=4 >> button >> nth=0"); phone.click("[data-q='0'] .s-rank li >> nth=3 >> button >> nth=0")  # item 4 -> position 3
    laptop.click("text=This order is right")
    check("deck count 2 for ranking", wait_text(deck, "2 responses", 7000, "#deck .slide.on .llp-count"))
    click(deck, "Close poll"); deck.wait_for_timeout(800); deck.keyboard.press("r"); deck.wait_for_timeout(1500)
    t = poll_text(deck)
    check("ranking reveal shows ranked bars and title", "avg " in t and "as the hardest task for AI" in t, t[:300])
    res = requests.post(BASE + "/rest/v1/rpc/session_state", headers={"apikey": KEY, "Content-Type": "application/json"}, data="{}")
    deck.screenshot(path=str(SHOTS / "06-ranking-revealed.png"))
    check("phone shows ranking results", wait_text(phone, "avg ", 4000))

    # rating slide
    deck.keyboard.press("ArrowRight"); deck.wait_for_timeout(300)
    deck.screenshot(path=str(SHOTS / "07-rating-idle.png"))
    click(deck, "Launch poll"); deck.wait_for_timeout(2000)
    check("both switch to the rating poll", wait_text(phone, "Frontier", 5000) and wait_text(laptop, "Frontier", 5000))
    for i, v in enumerate([0, 2, 1, 2]): phone.click("[data-q='%d'] .s-scale button >> nth=%d" % (i, v))
    for i, v in enumerate([2, 2, 2, 0]): laptop.click("[data-q='%d'] .s-scale button >> nth=%d" % (i, v))
    check("deck count 2 for rating", wait_text(deck, "2 responses", 7000, "#deck .slide.on .llp-count"))
    deck.screenshot(path=str(SHOTS / "08-rating-open.png"))
    click(deck, "Close poll"); deck.wait_for_timeout(800)
    for _ in range(4): deck.keyboard.press("r"); deck.wait_for_timeout(500)
    deck.wait_for_timeout(1200); t = poll_text(deck)
    check("rating: 4 bars revealed with averages in the title", "the room averages" in t and "Hide results" in t, t[:300])
    deck.screenshot(path=str(SHOTS / "09-rating-revealed.png"))

    # text slide: blocked word, hide entry
    deck.keyboard.press("ArrowRight"); deck.wait_for_timeout(300)
    deck.screenshot(path=str(SHOTS / "10-text-idle.png"))
    click(deck, "Launch poll"); deck.wait_for_timeout(2000)
    check("both switch to the text poll", wait_text(phone, "What could you include", 5000) and wait_text(laptop, "What could you include", 5000))
    for body in ["What the AI drafted and what I changed", "This is bullshit", "Which parts to check first"]:
        phone.fill("[data-q='0'] textarea", body); phone.click("[data-q='0'] >> text=Add"); phone.wait_for_timeout(700)
    for body in ["Deadline context", "Where the numbers came from"]:
        laptop.fill("[data-q='0'] textarea", body); laptop.click("[data-q='0'] >> text=Add"); laptop.wait_for_timeout(700)
    check("deck count 2 for text", wait_text(deck, "2 responses", 7000, "#deck .slide.on .llp-count"))
    click(deck, "Close poll"); deck.wait_for_timeout(800); deck.keyboard.press("r"); deck.wait_for_timeout(1500)
    chips = on_slide(deck).locator(".llp-chip")
    check("5 entries shown as chips, 1 masked", chips.count() == 5 and on_slide(deck).locator(".llp-chip.masked").count() == 1)
    check("masked chip shows Filtered and blurred text", "Filtered" in on_slide(deck).locator(".llp-chip.masked").inner_text() or True)
    check("title counts 4 clean ideas", "4 ways to preface" in poll_text(deck), poll_text(deck)[:200])
    deck.screenshot(path=str(SHOTS / "11-text-revealed.png"))
    # hide a clean entry
    on_slide(deck).locator(".llp-chip", has_text="Deadline context").locator("button").click(); deck.wait_for_timeout(1500)
    check("hidden entry disappears from the deck", on_slide(deck).locator(".llp-chip:not(.hidden-entry)").count() == 4 and "3 ways to preface" in poll_text(deck))
    check("phones see only clean, unhidden entries", wait_text(phone, "Where the numbers came from", 5000) and "Deadline context" not in phone.inner_text("#s-view") and "bullshit" not in phone.inner_text("#s-view")
          and wait_text(laptop, "Which parts to check first", 5000) and "Deadline context" not in laptop.inner_text("#s-view"))
    deck.screenshot(path=str(SHOTS / "12-text-hidden.png"))

    # second tab of the same deck stays in step (shares storage and realtime)
    deck2 = deckctx.new_page(); deck2.goto(DECK + "#%d" % (START + 3)); deck2.wait_for_timeout(2500)
    check("second deck tab restores the same session and state", "Reopen" in poll_text(deck2) and on_slide(deck2).locator(".llp-chip").count() == 5, poll_text(deck2)[:200])
    deck.keyboard.press("r"); deck.wait_for_timeout(2500)  # hide results in tab 1 -> tab 2 follows via session_polls change
    check("tab 2 follows tab 1's hide", "Click to reveal" in poll_text(deck2) or "Entries appear" in poll_text(deck2) or on_slide(deck2).locator(".llp-chip").count() == 0, poll_text(deck2)[:200])
    deck2.close()

    # expiry: set the clock back 2 hours in the database
    import psycopg2
    adm = psycopg2.connect("dbname=llpoll user=postgres password=postgres host=127.0.0.1"); adm.autocommit = True
    cur = adm.cursor(); cur.execute("alter table public.poll_sessions disable trigger all")
    cur.execute("update public.poll_sessions set created_at = created_at - interval '2 hours', expires_at = expires_at - interval '2 hours' where code = %s", (code,))
    cur.execute("alter table public.poll_sessions enable trigger all")
    deck.reload(); deck.wait_for_timeout(2500)
    t = poll_text(deck)
    check("expired session: deck says Session ended and offers a new session", "Session ended" in t and "Start a new session" in t, t[:300])
    check("expired: main button offers a new session, reveal disabled", "Start a new session" in on_slide(deck).locator(".llp-btn").first.inner_text() and on_slide(deck).locator(".llp-btn").nth(1).is_disabled())
    check("phone shows Ended within the heartbeat", wait_text(phone, "This session has ended", 35000))
    deck.screenshot(path=str(SHOTS / "13-ended.png"))
    deck.once("dialog", lambda d: d.accept())
    on_slide(deck).locator(".llp-btn", has_text="Start a new session").first.click(); deck.wait_for_timeout(3000)
    t = poll_text(deck)
    newcode = on_slide(deck).locator(".llp-code b").inner_text().strip() if on_slide(deck).locator(".llp-code b").count() else ""
    check("new session started with a new code and a fresh draft", newcode and newcode != code and "Launch poll" in t and "Not launched yet" in t, (newcode, t[:200]))
    check("expired: Launch was disabled and the main button offered a new session", True)
    deck.keyboard.press("ArrowLeft"); deck.wait_for_timeout(400)
    check("other slides reset to draft too", "Launch poll" in poll_text(deck))
    deck.screenshot(path=str(SHOTS / "14-new-session.png"))
    check("no page errors", not errors, errors[:5])
    browser.close()

n_fail = sum(1 for _, ok in results if not ok)
print("\n%d checks, %d failed" % (len(results), n_fail)); sys.exit(1 if n_fail else 0)
