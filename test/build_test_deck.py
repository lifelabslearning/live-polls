#!/usr/bin/env python3
"""Build test/deck.local.html: a 5-slide deck (title + one poll slide per type) that
exercises deck/llpoll.js against the local stack, the way html-presentations builds
real decks: definitions in <script type="application/json" id="llpoll-polls">,
each poll slide marked with data-poll="<id>", the libraries inlined ahead of the
controller, and a minimal engine (arrows, #cur, N toggles body.notes).

  python3 test/build_test_deck.py            -> test/deck.local.html (config/local.json)
  python3 test/build_test_deck.py --env dev  -> test/deck.dev.html   (a real-project smoke deck)

test_deck.py drives this deck, so its polls (prompts, options, 4 rating questions,
the free-text wall) are the ones the checks look for.
"""
import argparse, json, pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
TITLE = "Live polls test deck"
OPTS = ["Agree", "Unsure", "Disagree"]
POLLS = {
    "misconceptions": {
        "type": "choice", "title": "Agree or disagree?", "sub": "Vote on your phone. Results stay hidden until we reveal them.",
        "activeTitle": "{pct}% of you agree that {prompt_lc}", "activeOption": 0, "activeMode": "max",
        "questions": [
            {"prompt": "AI performs predictably across similar tasks.", "options": OPTS},
            {"prompt": "When AI increases performance, it also improves skill.", "options": OPTS},
            {"prompt": "AI productivity is the time saved generating an output.", "options": OPTS}]},
    "jagged": {
        "type": "ranking", "title": "Which is hardest for AI?", "sub": "Rank the five tasks on your phone, hardest first. Results stay hidden until we reveal them.",
        "activeTitle": "You ranked “{option}” as the hardest task for AI",
        "questions": [
            {"prompt": "Rank these tasks from hardest to easiest for AI.", "rankLabel": "hardest",
             "options": ["Count the r’s in strawberry", "Reply to an email", "Summarize text", "Generate code", "Write an essay"]}]},
    "flow": {
        "type": "rating", "title": "Rate your task on FLOW", "sub": "Score each dimension from 1 (low) to 3 (high) on your phone. Results stay hidden until we reveal them.",
        "activeTitle": "{lead}: the room averages {mean} of {max}",
        "questions": [
            {"prompt": "Frontier: how much more evidence do I need?", "scale": {"min": 1, "max": 3, "labels": ["Low", "High"]}},
            {"prompt": "Learning: how important is practicing the skill?", "scale": {"min": 1, "max": 3, "labels": ["Low", "High"]}},
            {"prompt": "Oversight: how much effort will AI require?", "scale": {"min": 1, "max": 3, "labels": ["Low", "High"]}},
            {"prompt": "Worst-Case: how consequential would an AI error be?", "scale": {"min": 1, "max": 3, "labels": ["Low", "High"]}}]},
    "preface": {
        "type": "text", "title": "What would you preface?", "sub": "Add up to 3 things on your phone that a recipient needs to know about AI-generated work. Entries stay hidden until we reveal them.",
        "activeTitle": "{count} ways to preface AI-generated work",
        "questions": [
            {"prompt": "What could you include when sharing AI-generated work so the recipient has the context they need?", "maxEntries": 3}]},
}
NOTES = ["Test deck."] + ["(POLL) Test slide for the %s poll." % k for k in POLLS]

ENGINE_CSS = """
*{box-sizing:border-box}html,body{margin:0;background:#111;font-family:'DM Sans',system-ui,sans-serif}
#deck{position:fixed;inset:0;display:flex;align-items:center;justify-content:center}
.slide{position:absolute;width:1280px;height:720px;overflow:hidden;background:#faf7ed;display:none}
.slide.on{display:block}
.title{padding:260px 72px 0;font:700 69px/1.08 'DM Sans',system-ui,sans-serif;color:#231f20}
#nav{position:fixed;bottom:10px;right:14px;color:#fff;font:500 12px/1 'DM Sans',sans-serif;background:rgba(0,0,0,.55);padding:7px 10px;border-radius:999px}
#notes{position:fixed;left:0;right:0;bottom:0;height:120px;background:#15171a;color:#e8e6e1;padding:12px 22px;display:none;white-space:pre-wrap}
body.notes #notes{display:block}
"""
ENGINE_JS = """
const slides=[...document.querySelectorAll('#deck .slide')];const NOTES=window.__NOTES__||[];let i=0;
function show(n){i=Math.max(0,Math.min(slides.length-1,n));slides.forEach((s,k)=>s.classList.toggle('on',k===i));
 document.getElementById('cur').textContent=i+1;document.getElementById('ntext').textContent=NOTES[i]||'';location.hash=String(i+1);}
addEventListener('keydown',e=>{
 if(['ArrowRight','PageDown',' ','Enter'].includes(e.key)){show(i+1);e.preventDefault();}
 if(['ArrowLeft','PageUp','Backspace'].includes(e.key)){show(i-1);e.preventDefault();}
 if(e.key==='n')document.body.classList.toggle('notes');
 if(e.key==='g')document.body.classList.toggle('grid');
 if(e.key==='Escape')document.body.classList.remove('grid');});
show(parseInt(location.hash.slice(1)||'1',10)-1);
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", choices=["local", "dev", "prod"], default="local")
    env = ap.parse_args().env
    cfg = json.loads((ROOT / "config" / f"{env}.json").read_text())
    deck = ROOT / "deck"
    llpoll_js = (deck / "llpoll.js").read_text()
    for token, val in (("__SUPABASE_URL__", cfg["url"]), ("__SUPABASE_KEY__", cfg["key"]), ("__PAGES_BASE__", cfg["pages_base"])):
        llpoll_js = llpoll_js.replace(token, val)
    libs = "\n".join((deck / n).read_text() for n in ("supabase.umd.js", "qrcode.js"))
    slides = ['<section class="slide"><div class="title">%s</div></section>' % TITLE]
    for pid, p in POLLS.items():
        slides.append('<section class="slide" data-slide="%s"><div class="poll poll--%s" data-poll="%s" data-poll-type="%s"><div class="llpoll"></div></div></section>'
                      % (pid, p["type"], pid, p["type"]))
    j = lambda o: json.dumps(o, ensure_ascii=False).replace("</", "<\\/")
    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>{TITLE}</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="poll-env" content="{env}">
<style>{ENGINE_CSS}</style>
<style>
{(deck / "llpoll.css").read_text()}
</style>
</head>
<body><div id="deck">{"".join(slides)}</div>
<div id="notes"><div id="ntext"></div></div>
<div id="nav"><b id="cur">1</b> / {len(slides)}</div>
<script>window.__NOTES__={j(NOTES)}</script>
<script>{ENGINE_JS}</script>
<script type="application/json" id="llpoll-polls">{j(POLLS)}</script>
<script>
{libs}
</script>
<script>
{llpoll_js}
</script>
</body></html>
"""
    out = ROOT / "test" / ("deck.local.html" if env == "local" else f"deck.{env}.html")
    out.write_text(html)
    print(f"wrote {out.relative_to(ROOT)} ({len(html):,} bytes, {len(slides)} slides, env={env})")


if __name__ == "__main__":
    main()
