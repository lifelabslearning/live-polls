# live-polls

Live polls for LifeLabs Learning workshops. Facilitators run a deck; participants answer on their phones.

- Respondent page: https://lifelabslearning.github.io/live-polls/ (`?s=CODE` joins a session; bare URL shows the join form; `?poll=UUID` is the legacy one-hour poll).
- Deck controller: `deck/llpoll.js` and `deck/llpoll.css`, inlined into decks by the `html-presentations` build.
- Database: Supabase, schema in `supabase/migrations/`.

## Layout
```
config/        dev.json, prod.json (publishable key only)
src/           respondent page sources
deck/          deck controller
legacy/        the original ?poll= page, unchanged
docs/          GitHub Pages output: index.html (prod), dev/index.html
supabase/      migrations
test/          Postgres stub, fake edge, Playwright suites, run_local.sh
build.py       assembles docs/ from src + legacy for one environment
```

## Build
```
python build.py --env dev
python build.py --env prod
python build.py --env local     # test/index.local.html, for the local stack
```
`build.py` expects `src/index.html` to contain `<!-- inline: session.css -->` and `<!-- inline: session.js -->` markers (and `<!-- inline-base64: <file> -->` for the font and mark) and the `__SUPABASE_*__` tokens somewhere in `src/`.

## Test
`bash test/run_local.sh` resets a local PostgreSQL 16 with the Supabase stand-ins and every migration, builds the respondent page and `test/deck.local.html` (a 5-slide deck built the way `html-presentations` builds real decks), starts the fake edge on :8787, and runs the SQL (159), respondent (55) and deck (54) suites. `--no-run` stops after starting the fake edge. Needs `pip install psycopg2-binary aiohttp requests playwright` and `python -m playwright install chromium`; CI runs the same script.

## How a session works
Deck: Start session (anonymous sign-in, 6-char code, QR) → Launch poll → Close → Reopen; Reveal / Reveal next / Hide (R key). Session kept in `localStorage['llsession:'+document.title]`; expires 2 hours after start (DB-enforced).
Phones: no sign-in; device token in localStorage, hash in DB; follow the deck via broadcast `llpoll:<CODE>` plus a 30 s heartbeat and 10 s fallback. Choice, rating and ranking autosave in 500 ms batches; text per entry. `blocked_terms` masks filtered words for everyone but the author.

## Releases
Tag `vX.Y.Z` on `main`. CI builds `docs/` and pushes the migration to production. `html-presentations` pins a tag with `scripts/update_llpoll.sh`.
