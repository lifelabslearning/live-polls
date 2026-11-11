# Changelog

## Unreleased
- 2026-11-08: removed `test/test_sessions_browser.js` (duplicate of `test_sessions_rest.py`).
- 2026-11-08: test stack rewired for the new layout: `build.py --env local`, `test/build_test_deck.py` (5-slide deck on the `#llpoll-polls` + `data-poll` hooks), `run_local.sh` applies `supabase/migrations/*.sql` in order and runs all three suites (159 + 55 + 54 pass); CI runs it on ubuntu-24.04. `release.yml` skips the prod build and migration push until `SUPABASE_PROD_PROJECT_REF` is set. `docs/.nojekyll` added.
- 2026-11-07: sources migrated from `presentation-components/live-polls-component` (src, deck, legacy, test, supabase-sessions.sql as 0001). `llpoll.js` reads poll definitions from `#llpoll-polls` + `data-poll` (the `.llp-config` path still works); `deck/` carries the supabase-js UMD and QR generator it needs; `build.py` inlines the font and mark from `src/` and serves `legacy/index.html` as `legacy.html`.
- New repo `live-polls` replaces `Live-Polls` (archived). Fresh history.
- `config/<env>.json` and `build.py --env`; tokens replace hardcoded constants.
- Migrations split into `supabase/migrations/`.
- `0002_abuse_caps.sql`: respondent cap per session, text length cap.

## Carried over from Live-Polls (shipped 2026-11-06)
- Session-based polls: anonymous deck sign-in, 6-char codes, QR join, device-token respondents.
- Batched autosave, word filter, Reveal / Reveal next / Hide, late-joiner behaviour.
- Test stack: 159 SQL, 55 respondent, 54 deck checks.
