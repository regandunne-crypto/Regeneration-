SUPABASE UPDATE REQUIRED
========================

Run the updated `supabase_schema.sql` in the Supabase SQL Editor.
It is idempotent, so it is safe to re-run as many times as you like.

What this version changes in the database:

1. quiz_tests and quiz_test_drafts gain a `default_time_limit` column
   (integer, default 30).
   Existing rows get 30 automatically, so every test you have already saved
   keeps running at exactly 30 seconds per question. Nothing to migrate.
   Per-question overrides live inside the existing `questions` JSONB column,
   so no schema change is needed for those.

2. A new table `quiz_game_results` stores a small record of each finished
   session: subject, test id and title, session name, timestamp, player count
   and the per-student answers. It deliberately does NOT copy the question
   text — it references the test id instead.

   This exists so a Render redeploy cannot destroy results you never
   downloaded. Your automatic spreadsheet download at the end of each game
   remains the authoritative record.

   Size: about 59 KB per 40-student, 20-question session, and only the 20 most
   recent sessions per subject are kept (older ones are deleted automatically).
   In practice this stays well under 2 MB of your 500 MB free plan. Set
   PERSIST_RESULTS=false in Render if you would rather not store them at all.

Environment variables in Render
-------------------------------

Still required:
- SUPABASE_URL
- SUPABASE_SERVICE_ROLE_KEY

NEW AND REQUIRED:
- APP_SESSION_SECRET

  On this first deploy, set APP_SESSION_SECRET to the CURRENT value of
  SUPABASE_SERVICE_ROLE_KEY. Your existing lecturer sessions are already
  signed with that value, so using it means nobody is signed out and no
  passwords change. You can rotate it to a fresh random value later, at a
  convenient moment — that signs everyone out once and they log back in with
  the same passwords.

  The server will refuse to start with SUPABASE_URL set and no
  APP_SESSION_SECRET, because the old fallback secret was written in the
  source code and could be used to forge a lecturer login.

Optional (see README_RENDER.txt for the full list):
- ALLOWED_EMAIL_DOMAINS   restrict lecturer signup to your university's domain
- SIGNUP_INVITE_CODE      a shared code for guest lecturers
- PERSIST_RESULTS         "false" to stop storing session results
- RESULTS_RETENTION       how many sessions to keep per subject (default 20)

Checking it worked
------------------

Sign in as a lecturer and open /api/diagnostics on your Render URL. It reports
whether each table (quiz_lecturers, quiz_tests, quiz_test_drafts,
quiz_subjects) is reachable, and shows the error if one is not. Use this first
whenever storage misbehaves.

After uploading this version to GitHub, Render should redeploy automatically.
Deploy between classes, not during one: students connected at the moment of
deployment are disconnected and have to rejoin.
