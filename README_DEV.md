# Developer guide

Everything you need to run the quiz platform on your own machine and to run the
test suite. Nothing here is required for the Render deployment — see
`README_RENDER.txt` for that.

Python 3.11 or newer (Render currently uses 3.13).

## 1. Set up

```bash
python -m venv .venv
# Windows (Git Bash):   source .venv/Scripts/activate
# Windows (PowerShell): .venv\Scripts\Activate.ps1
# macOS / Linux:        source .venv/bin/activate

pip install -r requirements.txt -r requirements-dev.txt
```

## 2. Run locally

```bash
uvicorn server:app --reload
```

Then open <http://localhost:8000>.

- Students land on the subject picker.
- Lecturers click **"Lecturer? Host a game"**, or go straight to
  <http://localhost:8000/#host>.

The first time you run it there are no lecturer accounts. Click **Create
Account** on the sign-in screen — signup is open unless you set
`ALLOWED_EMAIL_DOMAINS` or `SIGNUP_INVITE_CODE` (see below).

**Keep it to one worker.** Live game state (`rooms`, `session_tokens`,
`last_game_stats`) lives in process memory. `uvicorn server:app` with the default
single worker is required; adding Gunicorn workers would split the game rooms
across processes and break every live session.

## 3. The two storage modes

The app uses `HybridTestRepository`, which picks a backend at import time:

| Mode | When | Durability |
|---|---|---|
| `supabase` | `SUPABASE_URL` **and** `SUPABASE_SERVICE_ROLE_KEY` are both set | Durable. This is production. |
| `local-file` | Neither is set (the default for local dev) | Survives restarts; written to `local_store.json` next to `server.py`. **Wiped on every Render redeploy** — Render's free tier has no persistent disk. |
| `in-memory` | The local file cannot be written | Lost on restart. |

Point the local file somewhere else with `LOCAL_STORE_PATH`:

```bash
LOCAL_STORE_PATH=/tmp/quiz_store.json uvicorn server:app --reload
```

To develop against a real Supabase project, apply `supabase_schema.sql` in the
Supabase SQL editor first, then:

```bash
export SUPABASE_URL=https://yourproject.supabase.co
export SUPABASE_SERVICE_ROLE_KEY=...        # service role, not anon
export APP_SESSION_SECRET=some-long-random-string
uvicorn server:app --reload
```

Signed in as a lecturer, `GET /api/diagnostics` reports which tables are actually
reachable — use it first whenever storage behaves oddly.

## 4. Run the tests

```bash
pytest              # whole suite
pytest -q           # quiet
pytest tests/test_drafts.py -v
pytest -k draft     # by name
```

Every test reloads `server.py` fresh against its own `tmp_path` store, so tests
never touch your real `local_store.json` and never see each other's data. A
"server restart" is simulated by reloading the module against the same store
file — that is how the draft-durability tests work.

Classroom pacing (the 3 s get-ready, 5 s reveal, 20 s entry-code countdown) is
collapsed to milliseconds in tests via `GET_READY_SECONDS`, `REVEAL_SECONDS` and
`GAME_CODE_COUNTDOWN_SECONDS`. You can set those in a real run too if you want to
rehearse a quiz quickly.

| File | Covers |
|---|---|
| `tests/test_auth.py` | signup → login → session cookie round trip |
| `tests/test_tests_crud.py` | create / list / get / update / delete a test |
| `tests/test_drafts.py` | draft save → read back → survives a restart |
| `tests/test_game_flow.py` | full two-player game over WebSockets + stats download |
| `tests/test_timing.py` | per-question and per-test time limits |
| `tests/test_import_docx.py` | Word import parsing and the downloadable template |
| `tests/test_security.py` | auth gates, signup restrictions, identity tokens |
| `tests/test_results.py` | stored game results, pruning, answer distribution, student review |
| `tests/test_bugfixes.py` | regressions for the Phase 3 fixes |
| `tests/test_draft_logic.js` | the draft decision logic, run under Node |

The JavaScript tests run under `pytest` via `tests/test_draft_logic.py`, or
directly with `node --test tests/test_draft_logic.js`. They are skipped (not
failed) if Node is not installed.

## 5. Lint

```bash
ruff check .
ruff check . --fix     # only the mechanically safe fixes
```

`ruff check .` must be clean before you commit. Config is in `pyproject.toml`;
`E501` (line length) is deliberately off because the existing code is written
wide and reflowing it would bury real changes in noise.

## 6. Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `SUPABASE_URL` | — | Supabase project URL. Enables Supabase storage when paired with the key below. |
| `SUPABASE_SERVICE_ROLE_KEY` | — | Service role key. Bypasses RLS; never expose it to the browser. |
| `APP_SESSION_SECRET` | falls back to the service role key | HMAC key for lecturer session cookies. **Required** when `SUPABASE_URL` is set. |
| `LOCAL_STORE_PATH` | `./local_store.json` | Where local-file storage writes. |
| `REQUIRE_SUPABASE` | `false` | Refuse writes when Supabase is configured but unreachable, instead of silently falling back to local. |
| `ALLOWED_EMAIL_DOMAINS` | — | Comma-separated allowlist for lecturer signup, e.g. `tut.ac.za`. Unset = signup open. |
| `SIGNUP_INVITE_CODE` | — | Shared code accepted for lecturer signup. Unset = not required. |
| `ALLOWED_ORIGINS` | deployment origin | CORS allowlist. `*` is opt-in only. |
| `PERSIST_RESULTS` | `true` | Store a lightweight row per finished game. |
| `RESULTS_RETENTION` | `20` | Keep this many recent sessions per subject; older ones are pruned. |
| `DRAFT_RATE_LIMIT` | `120/minute` | Draft autosave limit. Keep it generous — autosave fires 1.5 s after typing stops. |
| `TIME_PER_Q` | `30` | Fallback seconds per question, for tests saved before per-test timing existed. |
| `GET_READY_SECONDS` | `3` | Countdown before each question. |
| `REVEAL_SECONDS` | `5` | Pause on the answer reveal before auto-advancing. |
| `GAME_CODE_COUNTDOWN_SECONDS` | `20` | Entry-code display time before the game starts. |
| `MAX_PLAYERS_PER_ROOM` | `300` | Per-session student cap. |
| `MAX_WS_MESSAGE_BYTES` | `65536` | Largest accepted WebSocket frame. |
| `PORT` | `8000` | Set by Render. |

## 7. Front end

There is **no build step**. `app.js` is plain ES2020 loaded directly by
`index.html`, and `style.css` is hand-written. Edit and reload. Keep it that way
unless there is a concrete reason to add tooling.

Screens are `.screen` divs in `index.html`, shown one at a time by
`showScreen(id)`. The same file serves both the student and lecturer flows; the
`isHost` flag decides which message handler runs.
