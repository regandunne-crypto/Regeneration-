Render deployment
=================

1. Create a new GitHub repository and upload these files.
2. In Render, choose New + > Web Service.
3. Connect the GitHub repository.
4. Render should detect render.yaml automatically. If not, use:
   Build Command: pip install -r requirements.txt
   Start Command: uvicorn server:app --host 0.0.0.0 --port $PORT
5. Set the environment variables below.
6. After deploy, open the Render URL.
7. Students use the main URL. Lecturers use the same URL with #host at the end.

Keep it on ONE worker. Live game state (rooms, session tokens, the last game's
results) is held in process memory. Adding Gunicorn workers would split game
rooms across processes and break every live session.


READ THIS BEFORE YOUR FIRST DEPLOY OF THIS VERSION
--------------------------------------------------

APP_SESSION_SECRET is now required whenever SUPABASE_URL is set. The server
refuses to start without it, because the old fallback was a secret written in
this source code — anyone who read it could forge a lecturer login.

On this first deploy, set APP_SESSION_SECRET to the CURRENT value of
SUPABASE_SERVICE_ROLE_KEY.

That is what your existing lecturer sessions are already signed with, so setting
it to the same value means nobody is signed out and nobody has to sign in again.
Later, at a convenient moment (not during a class), you can change it to a fresh
random value; that will sign everyone out once, and they simply log back in with
their existing passwords.

Two other notes on this deploy:

- Deploy the student-identity change BETWEEN classes, not during one. Students
  connected at that moment are disconnected and have to rejoin.
- No passwords are changed, re-hashed or invalidated. Existing accounts keep
  working exactly as they are.


Environment variables
---------------------

Required in production:

  SUPABASE_URL                Your Supabase project URL.
  SUPABASE_SERVICE_ROLE_KEY   Service role key. Server-side only — it bypasses
                              row level security and must never reach a browser.
  APP_SESSION_SECRET          Signs lecturer session cookies. See above.

Optional:

  ALLOWED_EMAIL_DOMAINS   Comma-separated list, e.g. "tut.ac.za". Only these
                          email domains may create a lecturer account.
                          Subdomains are accepted (eng.tut.ac.za matches
                          tut.ac.za). Colleagues sign themselves up with their
                          work email — no admin work for you.
                          LEAVE UNSET AND SIGNUP STAYS OPEN, exactly as before.

  SIGNUP_INVITE_CODE      A shared code that also allows signup. Use it as well
                          as, or instead of, the domain list — for a guest
                          lecturer without a university address.
                          If neither this nor ALLOWED_EMAIL_DOMAINS is set,
                          anyone can create an account (the current behaviour).

  ALLOWED_ORIGINS         CORS allowlist. Defaults to the deployment origin,
                          which is all this app needs since it is served
                          same-origin. Set to "*" only if you deliberately want
                          to open the API to other sites.

  PERSIST_RESULTS         "true" (default) stores a small row per finished game
                          so results survive a redeploy. Set to "false" to turn
                          it off entirely.
  RESULTS_RETENTION       How many recent sessions to keep per subject
                          (default 20). Older ones are pruned automatically.

  REQUIRE_SUPABASE        "true" makes the server refuse writes when Supabase is
                          unreachable, instead of quietly falling back to a
                          local file that Render wipes on redeploy.

  TIME_PER_Q              Fallback seconds per question (default 30) for tests
                          saved before per-test timing existed.
  REVEAL_SECONDS          Pause on the answer reveal (default 5).
  GET_READY_SECONDS       Countdown before each question (default 3).
  GAME_CODE_COUNTDOWN_SECONDS   Entry-code display time (default 20).

  MAX_PLAYERS_PER_ROOM    Per-session student cap (default 300).
  DRAFT_RATE_LIMIT        Draft autosave limit (default "120/minute"). Keep it
                          generous: autosave fires 1.5 s after typing stops.


Storage
-------

With SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY set, tests, drafts, lecturer
accounts and subjects live in Supabase and survive redeploys.

Without them the server falls back to local_store.json next to server.py. On
Render's free tier that file is WIPED ON EVERY REDEPLOY, because there is no
persistent disk. The lecturer library screen shows which mode is active, and
the draft status line says where each draft was actually stored.

Signed in as a lecturer, open /api/diagnostics to see exactly which Supabase
tables are reachable. Use that first if anything storage-related misbehaves.


Results and privacy
-------------------

The results spreadsheet contains student names, student numbers and
per-question answers. /api/stats/ now requires a signed-in lecturer. The
automatic download at the end of a game is unaffected — the host browser is
already signed in and sends its session cookie with the request.

That automatic download to your PC remains the authoritative record of a
session. Stored results are a convenience copy and are pruned automatically.
