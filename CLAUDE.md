# live-polls

Runtime for LifeLabs live polls: the respondent page every phone loads, the deck controller every deck inlines, and the Supabase schema. Public repo; contains no workshop content.

## Commands
- `test/run_local.sh` resets the local stack (Postgres 16 stub + fake edge) and runs the SQL, respondent and deck suites. Run before every commit.
- `python build.py --env dev` writes `docs/dev/index.html`; `--env prod` writes `docs/index.html`. GitHub Pages serves `docs/`.
- Migrations: `supabase db push` against the linked project, only after a yes (see root CLAUDE.md gates).

## Conventions
- `config/<env>.json` holds `url`, `key` (publishable) and `pages_base`. Source files carry the tokens `__SUPABASE_URL__`, `__SUPABASE_KEY__` and `__PAGES_BASE__`; the builds substitute them. Never hardcode a project URL in `src/` or `deck/`.
- `supabase/migrations/NNNN_name.sql`, numbered, never edited after they run on prod. New change = new file.
- Tags are `vMAJOR.MINOR.PATCH`. Bump MINOR for a change decks must rebuild to use, PATCH for respondent-page-only fixes. Every tag gets a CHANGELOG line.
- `legacy/index.html` is the one-hour `?poll=` page; build copies it in unchanged. Do not refactor it.

## Decisions (do not reopen)
- No recovery secret / `claim_session`; no Run again (`run` column, `archived` status reserved).
- Late joiner between polls sees the last closed poll; Waiting only before the first poll.
- github.io, no custom domain for now. Keep 30 s heartbeat and 10 s fallback.
- Phones never sign in: device token in localStorage, SHA-256 hash in DB.
