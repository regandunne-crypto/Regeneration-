# Changes

Work order carried out in seven phases, one commit each, so any phase can be
rolled back on its own. The test suite passes after every phase: **133 tests**
(129 Python + a Node suite for the draft decision logic), and `ruff check .` is
clean.

**Before you deploy, read the first section of `README_RENDER.txt`.** One new
environment variable is required in production and there is a specific value to
use on the first deploy so that nobody is signed out.

---

## Phase 1 — Test infrastructure and lint baseline

There was previously no way to verify a change, which is why bugs survived.

**Added**
- A `pytest` suite covering lecturer auth, test CRUD, draft durability and a
  full two-player game over WebSockets ending in the stats spreadsheet.
- Tests reload `server.py` fresh against a `tmp_path` store, so they are
  hermetic and can simulate a server restart by reloading against the same
  store file.
- `GET_READY_SECONDS`, `REVEAL_SECONDS` and `GAME_CODE_COUNTDOWN_SECONDS`, so a
  whole game runs in milliseconds under test. Defaults unchanged (3 / 5 / 20).
- `requirements-dev.txt`, `pyproject.toml` (ruff + pytest config), `.gitignore`
  and `README_DEV.md`.

**Fixed (found by the new tests)**
- Invalid question payloads crashed the response instead of returning a
  validation message. Pydantic puts the raw `ValueError` in `ctx` and FastAPI
  cannot serialise it, so "Each question must have exactly 4 options" reached
  the lecturer as a dead request.
- `datetime.utcnow()` (deprecated on Python 3.12+) replaced with timezone-aware
  `datetime.now(UTC)`. Stored timestamps were naive, so the browser read them as
  local time and showed draft save times off by the UTC offset.

---

## Phase 2 — Save Draft (the main complaint)

### Confirmed root cause

Two faults in the browser made a correctly stored draft unreachable:

1. `showCreateTestScreen()` fired an **unconditional `DELETE /api/drafts/{subject}`**
   in create mode. Clicking "Create New Test" destroyed the draft saved in the
   previous session *before it was ever read*.
2. A draft was only restored in `edit` mode, and only when its `editing_test_id`
   matched the test being edited. A draft built while creating a **new** test has
   `editing_test_id = null`, so it could never match and never be restored.

That is exactly why the ordinary Save button worked while Save Draft appeared to
do nothing: Save writes a *test*, and nothing deletes a test on the way back in.

Three further problems hid the failure: the save endpoint returned HTTP 200 with
`{"ok": false}` on error while the client only checked the HTTP status (so a
failed save was reported as "Draft saved"), the repository swallowed every
Supabase exception and fell back to Render's ephemeral file, and nothing in the
UI ever told the lecturer a draft existed.

### Fixed

- Decision logic extracted into `draft_utils.js` (pure, no DOM) and unit tested
  with `node --test`; `pytest` runs those tests too. **No decision path can
  discard a draft on its own** — there is a test asserting exactly that.
- The editor always reads the draft on the way in, in both modes, and then
  resumes, warns, or offers **Resume draft / Start blank (discards draft)**. A
  draft is only ever destroyed after an explicit choice.
- A **"Draft in progress"** card at the top of the test library with Resume and
  Discard. Previously there was no way to reach a stored draft at all.
- A **`localStorage` mirror**, written before every network call. It survives
  server restarts, Supabase outages and Render's ephemeral disk, and is offered
  when it is newer than the server copy.
- `POST /api/drafts` returns a real 500 when the write genuinely failed, and
  `saveDraft()` checks `resp.ok` as well as the HTTP status.
- The response reports which backend actually stored the draft, so the status
  line reads "Draft saved to Supabase 14:32" or "Draft saved locally — it will
  be lost if the server redeploys".
- `GET /api/diagnostics` (lecturer auth) probes `quiz_lecturers`, `quiz_tests`,
  `quiz_test_drafts` and `quiz_subjects` and reports reachable/missing plus the
  error text.
- The pending autosave timer is cleared and flushed whenever the editor is left,
  and a `beforeunload` guard warns while the draft is dirty.

### Is the resume flow still confusing?

No, and Save Draft should stay. The flow is now: open the editor, and if a draft
exists you are asked one plain question with two buttons. Otherwise nothing
appears. The library card means a draft is never invisible. The alternative you
raised — removing Save Draft entirely — is not needed, and would lose work on
long quizzes where the browser or the server drops out mid-build.

---

## Phase 3 — Bug fixes

Each item was checked against the source before being touched.

| # | Status |
|---|---|
| 1. Double question advance | **Fixed.** No guard existed; a double click or a click racing the auto-reveal timer incremented `current_q` twice and skipped a question. Per-room `asyncio.Lock` plus an expected-index check. |
| 2. Double reveal | **Not reproducible as described.** There is no `await` between the `phase == "question"` check in `maybe_finish_question_early()` and the phase change inside `auto_reveal()`, so two concurrent answer handlers cannot both get through on a single event loop. The same lock now covers it anyway, so a future refactor that introduces an await cannot reopen it. |
| 3. `start_game` blocked the host socket for 20 s | **Fixed.** The countdown and get-ready sleep moved into a background task. |
| 4. Dict mutated during async iteration | **Fixed.** Snapshot iteration; entries deleted mid-call are skipped. |
| 5. N+1 queries on `/api/subjects` | **Fixed.** One query for all subjects instead of one per subject. |
| 6. `datetime.utcnow()` | **Fixed in Phase 1** (needed to make ruff pass). ISO strings verified to parse in the browser — and now *more* correctly, since they carry an offset. |
| 7. Password hashing blocked the event loop | **Fixed.** 260k PBKDF2 rounds moved to a worker thread; a class signing in no longer freezes a live quiz. |
| 8. Unvalidated answer choice | **Fixed.** Must be an `int` in range. Rejected with `invalid_answer`, not `error` — the player client treats `error` as a join failure and would bounce the student to the join screen. |
| 9. Timer percentage used a hardcoded constant | **Fixed.** Both the countdown and the bar percentage use the server-provided limit. |
| 10. Duplicate `id="btn-pause-game"` | **Fixed.** Converted to a `.btn-pause-game` class. |
| 11. Mislabelled button | **Fixed.** A built-in test read "Edit Test" but carried the duplicate class; now "Copy & Edit". |
| 12. Player could not change subject | **Fixed.** The back link shows when the student arrived via subject selection (not via a QR token, where the lecturer fixes the subject). |
| 13. Confusing rejoin state | **Fixed.** A proper "Answer submitted — waiting for other students" screen. |
| 14. Unguarded `JSON.parse` | **Fixed.** Both the parse and the handler are guarded. |
| 15. Leaked intervals | **Fixed in Phase 2**, tracked and cleared on every screen change. |
| 16. `GameRoom.__init__` `KeyError` | **Fixed.** Falls back to the subject code as the name. |
| 17. Resource limits | **Added.** `MAX_WS_MESSAGE_BYTES` (64 KB) and `MAX_PLAYERS_PER_ROOM` (300). |

**Also fixed, found while adding the advance guard:** reaching `force_end_game`
from inside the question timer made `cancel_question_timer()` cancel and await
the currently running task, raising `RuntimeError: Task cannot await on itself`.
This broke the end of any game whose last question timed out with nobody
answering. Covered by `test_game_ends_cleanly_when_the_last_question_times_out`.

---

## Phase 4 — Security and privacy

1. **`/api/stats/` requires lecturer auth.** It returns student names, student
   numbers and per-question results and was downloadable by anyone who knew a
   subject code. **The end-of-game automatic download still works** — verified by
   a test; the host browser is signed in and sends the cookie same-origin.
2. **Student identity is server-signed.** `visitorId` came off a query parameter,
   so a student could supply someone else's id and inherit their score. The
   server now mints the id, signs it, and only accepts it back inside the
   signature. A bare `visitorId` is ignored.
3. **Signup gating.** `ALLOWED_EMAIL_DOMAINS` (preferred; subdomains accepted, so
   colleagues self-serve with their work email) and `SIGNUP_INVITE_CODE` as an
   alternative or addition. **If neither is set, signup stays open exactly as
   today.** The signup screen names the accepted domain on rejection.
4. **Session secret.** The server refuses to start when `SUPABASE_URL` is set and
   `APP_SESSION_SECRET` is missing, rather than signing sessions with a secret
   written in this source file.
5. **PostgREST filter injection.** A single choke point in `_request` refuses any
   filter value containing PostgREST syntax characters, plus `safe_test_id` /
   `safe_subject_code` validation at the API boundary.
6. **CORS** defaults to the deployment origin; `*` is opt-in.
7. **Rate limits** on test create (30/min) and update (60/min). Drafts get a
   deliberately generous `DRAFT_RATE_LIMIT` (default 120/min) because autosave
   fires 1.5 s after typing stops — a tight limit would break the feature
   Phase 2 exists to fix. A test saves 40 drafts in a row to prove it.
8. **`/api/health` and `/api/storage-status`** no longer return raw Supabase
   error text to anonymous callers.
9. **Stale note removed** from `README_RENDER.txt` about a lecturer passcode in
   `app.js` — that mechanism no longer exists.

### What this means for lecturers signing in

Verified by tests, not assumed:

- **No password is changed, re-hashed or invalidated.** Existing accounts work
  exactly as they are. Nobody needs a password reset.
- **Nobody is signed out**, provided `APP_SESSION_SECRET` is set to the *current*
  `SUPABASE_SERVICE_ROLE_KEY` value on the first deploy. That is what existing
  session cookies are already signed with.
  `test_seeding_the_secret_with_the_service_role_key_keeps_sessions_valid`
  asserts this. If you set a fresh random value instead, everyone is signed out
  once and simply logs back in with their existing password.
- **Colleagues can still create their own accounts** with no admin work — that is
  why the domain allowlist is preferred over manual approval.
- **Test ownership is unchanged.** Lecturers still see all tests for a subject
  and can edit only their own. Deliberately not tightened.
- **Deploy the identity change between classes.** Students connected at that
  moment are disconnected and have to rejoin.

---

## Phase 5 — Word document import

### What it will handle

- Numbering: `1.` `1)` `Q1.` `Question 1:`, none at all, and **Word's automatic
  list numbering**. That last one matters most — the number lives in `pPr/numPr`
  and never appears in the text `python-docx` returns, so a parser looking for
  "1." finds nothing. Tested against real `numPr` markup, including a document
  where the *options* are auto-numbered and carry no letters either.
- Option markers `A)` `A.` `(A)` `[A]` `a)`, and unmarked Word list paragraphs.
- Correct answer via `Answer:` / `Ans:` / `Correct:` / `Key:`, by letter **or by
  the option's own text**, or an asterisk on the correct option.
- `Explanation:` / `Rationale:` / `Feedback:` / `Why:` / `Reason:`.
- An optional `Time: 60` line per question, mapped to the Phase 6 override.
- A **table layout** (Question / A / B / C / D / Answer / Explanation / Time),
  preferred over prose. A document containing both is merged and de-duplicated.
- Unicode preserved: θ Σ ° µ ± × m⁴ ·. Smart quotes, en/em dashes, ellipses and
  non-breaking spaces are normalised; nothing non-ASCII is stripped. Superscripts
  applied as Word *formatting* are converted to Unicode, so "m⁴" does not
  silently flatten to "m4".

### What it will not handle

- **Equations built with Word's equation editor cannot be imported.** In plain
  language: if you used Word's *Insert → Equation* tool, that content is stored
  as a drawing-like object rather than as text, and it is invisible to any text
  extraction. Rather than dropping it silently, the import flags every question
  containing one: *"This question contains a Word equation that could not be
  imported — please retype it as text."* Type such formulas as ordinary text
  (`σ = F / A`) and they import perfectly. Ordinary symbols typed with Insert →
  Symbol, or pasted in, are fine.
- Images and diagrams are not imported (see Phase 7b, deferred).
- Questions with 2 or 3 options are **skipped with a warning**, never padded.
- A block is only accepted as a question if something marks it as one — lettered
  options, an `Answer:` line, or an asterisk. Without that rule, headings and
  bulleted prose parse as questions. Ordinary lecture notes import zero
  questions and produce zero warnings.

Limits: 5 MB, 200 questions. The endpoint **only returns JSON** — it never writes
a test; the lecturer reviews everything in a modal (Append / Replace all /
Cancel) before saving. `GET /api/import/template` generates the template `.docx`
at request time, so no binary is committed; a test parses it back and asserts the
questions survive.

---

## Phase 6 — Configurable question timing

`TIME_PER_Q` becomes a fallback default rather than a hard constant.

- `default_time_limit` on `quiz_tests` and `quiz_test_drafts`, default 30.
  Optional per-question `time_limit` inside the existing `questions` JSONB.
- Resolution: question level → test level → `TIME_PER_Q`. Bounds 5–300 s, with a
  clear validation message outside them.
- **Backward compatible.** Existing tests have neither field and keep running at
  exactly 30 seconds with no migration — there is a test that strips both fields
  and asserts it.
- Scoring uses the resolved limit, so a fast answer on a 120 s question still
  scores near 1000. Tested at 10 s, 30 s and 120 s.
- Mid-question reconnects show the right remaining time against the right limit.
- Editor: a test-level default with presets and a custom value, a per-question
  override whose "Use test default (Ns)" label updates live, and a running
  **estimated quiz length**. Timing is included in both `collectTestFormPayload`
  and `collectDraftFormPayload` — there is a test for the draft round trip,
  because omitting it there is an easy bug.
- Host: the current question's limit is shown, and **"+15 seconds"** extends the
  live question.

---

## Phase 7a — Completed

- **Persisted game results.** A lightweight row per session in
  `quiz_game_results` — no question text, just a reference to the test id.
  Pruned after every insert via `RESULTS_RETENTION` (default 20 per subject) and
  switchable off with `PERSIST_RESULTS`. **The automatic download at the end of a
  game is unchanged and remains the authoritative record.** The library screen
  shows how many sessions are stored and roughly how large they are.

  **Measured cost: 59.3 KB for a 40-student, 20-question session** — 0.0116% of
  the 500 MB free plan. At 150 sessions a year that is ~8.7 MB/year, and with
  20-session pruning the stored total never exceeds ~1.2 MB per subject. This is
  measured by `test_measured_storage_cost_per_session`, not estimated.

- **Answer distribution on the reveal screen.** The host sees how many students
  picked each option, with the correct one highlighted.
- **Post-game student review.** Students can see which questions they got wrong,
  with the correct answer and the explanation. Each student receives only their
  own data.
- **Supabase pause guard.** Free projects are paused after 7 days of inactivity.
  The app now detects the unreachable state and says "The question database
  appears to be asleep — open your Supabase dashboard and press Resume" instead
  of a generic failure.

## Phase 7b / 7c — Deliberately not done

Deferred so that phases 1–7a ship as a coherent, tested whole. None of these are
blocked; each is a self-contained piece of work.

| Item | Why deferred |
|---|---|
| Images per question | Needs Supabase Storage, an upload path, size limits and rendering on three screens. The largest remaining feature and worth its own phase. Probably the highest-value next item for mechanics. |
| More question types (True/False, multiple-correct, numeric with tolerance) | Each changes the answer model, the scoring path, the player UI and the spreadsheet. Numeric-with-tolerance is the most valuable for engineering and would suit being done first, on its own. |
| Question bank + random selection | Needs a pool data model separate from tests, and a policy for how a drawn set relates to stored results. |
| Export a test to Word/PDF | Straightforward now that `docx_import` exists and the format is settled, but adds no capability the lecturer lacks today. |
| MathJax/LaTeX rendering | Explicitly deferred in the brief; revisit only if equation-heavy questions become common. The Word-equation warning covers the current gap. |
| Class list upload (CSV) | Would catch mistyped student numbers, which currently corrupt the spreadsheet silently. Small and useful — the best 7c candidate. |
| Team mode | Substantial changes to scoring, the leaderboard and the spreadsheet. |
| Practice mode | Effectively a second game loop without a host. |
| Accessibility pass | Keyboard answer selection (1–4), contrast verification and `prefers-reduced-motion`. Should be done as one focused pass rather than piecemeal. The existing coloured shapes, which help colour-blind students, were left untouched throughout. |
| Cross-session analytics | Now cheap, since Phase 7a stores per-session results. A natural follow-on. |

---

## Files added

| File | Purpose |
|---|---|
| `draft_utils.js` | Pure draft decision logic, unit tested |
| `docx_import.py` | Word parsing and template generation |
| `tests/` | 133 tests |
| `requirements-dev.txt`, `pyproject.toml`, `.gitignore` | Dev tooling |
| `README_DEV.md` | Local dev and testing |
| `CHANGES.md` | This file |

## Files changed

`server.py`, `app.js`, `index.html`, `style.css`, `supabase_schema.sql`,
`requirements.txt`, `render.yaml`, `README_RENDER.txt`,
`README_SUPABASE_SETUP.txt`.

## What must be re-run

Paste the updated **`supabase_schema.sql`** into the Supabase SQL editor. It is
idempotent and safe to re-run. It adds `default_time_limit` to `quiz_tests` and
`quiz_test_drafts` (existing rows default to 30, so nothing changes for saved
tests) and creates `quiz_game_results`.
