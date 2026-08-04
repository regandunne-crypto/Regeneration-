# Claude Code Work Order — Engineering Quiz Game (mechanics_gameR5)

Paste this whole file to Claude Code as the task brief. Work through the phases **in order** and commit
after each phase so anything can be rolled back independently.

---

## 0. Context

A real-time multiplayer quiz platform (Kahoot-style) for engineering subjects, used by a lecturer with
university students.

| File | Lines | Role |
|---|---|---|
| `server.py` | ~2670 | FastAPI + WebSocket server, Supabase/local hybrid storage, Excel export |
| `app.js` | ~2460 | All front-end logic (player + host/lecturer) |
| `index.html` | ~500 | All screens as `.screen` divs, toggled by `showScreen()` |
| `style.css` | ~2200 | Styling |
| `supabase_schema.sql` | 129 | Idempotent schema, run in the Supabase SQL editor |
| `render.yaml` | 13 | Render deploy config (free tier, no persistent disk) |

Architecture notes you must respect:

- Storage is a **hybrid**: `HybridTestRepository` uses Supabase when `SUPABASE_URL` +
  `SUPABASE_SERVICE_ROLE_KEY` are set, otherwise a local `local_store.json`, otherwise in memory.
  On Render's free tier the local file is **ephemeral** — it is wiped on every redeploy.
- Lecturer auth is a signed HMAC session cookie (`lecturer_session`), not a Supabase JWT. The backend
  uses the service role key and bypasses RLS.
- Live game state (`rooms`, `session_tokens`, `last_game_stats`) is **in-process memory only**. This
  means the app must stay single-worker. Do not add Gunicorn workers without solving this first.
- The front end has no build step. Plain ES2020 in one file, loaded directly. Keep it that way unless
  a phase below says otherwise.

**Before changing anything**, read `server.py`, `app.js` and `index.html` in full so you understand the
host flow: subject → test library → editor / lobby → question → reveal → final.

---

## 1. Environment and verification setup (do this first)

There is currently no way to test this thing, which is why bugs have survived. Set that up before
touching application code.

1. Create `requirements-dev.txt` with `pytest`, `pytest-asyncio`, `httpx`, `ruff`.
2. Add a `tests/` directory with `pytest` tests using FastAPI's `TestClient` and its WebSocket test
   client. Cover at minimum:
   - lecturer signup → login → session cookie round trip
   - create test → list tests → get test → update test → delete test
   - **draft save → draft read-back → draft survives a simulated restart** (see Phase 2)
   - a full two-player game over WebSockets: host_join, two player_joins, start_game, both answer,
     early reveal, advance, final leaderboard, stats download
3. Add `LOCAL_STORE_PATH` support to the tests via `tmp_path` so tests never touch a real store.
4. Add a `README_DEV.md` explaining how to run locally (`uvicorn server:app --reload`), how to run
   tests, and the two storage modes.
5. Confirm `ruff check .` passes on `server.py` (fix genuine findings, do not mass-reformat).

Tests must pass before and after every later phase.

---

## 2. Fix the draft save (highest priority — this is the user's main complaint)

### Reported symptom
"I click Save Draft, exit, come back in, and there is no saved draft — I have to start again. The
normal Save button works fine."

### Root cause — already diagnosed, verify then fix
The server side is largely fine. The bug is in the browser and in silent error handling:

1. **`app.js` `showCreateTestScreen()` (~line 1631):** when `editorMode === 'create'` it fires
   `DELETE /api/drafts/{subject}` unconditionally. So clicking "Create New Test" destroys the draft the
   lecturer saved last session, before it is ever read.
2. **`app.js` `showCreateTestScreen()` (~line 1649):** a draft is only loaded in `edit` mode, and only
   when `getDraftEditingId(draft) === editingTestId`. A draft created while building a *new* test has
   `editing_test_id === null`, so it can never match and can never be restored.
3. **`server.py` `save_test_draft()` (~line 1656):** on any exception it returns **HTTP 200** with
   `{"ok": false, "draft": null}`. `app.js` `saveDraft()` (~line 1569) only checks the HTTP status via
   `parseApiResponse`, so it ignores `ok: false` and cheerfully shows "Draft saved <timestamp>." The
   lecturer is told the save succeeded when it failed.
4. **`server.py` `HybridTestRepository.save_draft()` (~line 1087):** catches *every* exception from
   Supabase and silently falls back to `local_store.json`. On Render's free tier that file is ephemeral,
   so drafts written there vanish on redeploy while the UI reports success.
5. **No entry point to resume.** Nothing in the test library tells the lecturer a draft exists or offers
   to open it. Even a correctly stored draft is unreachable.
6. **Pending-timer race.** `draftSaveTimer` is not cleared when leaving the editor, so a queued autosave
   can fire after navigation and re-save stale form contents over a newer state.

### Required fix

- Drafts are keyed `(lecturer_id, subject_code)` — one per subject. **Keep that key**, but make the
  create-mode flow work with it:
  - Remove the unconditional `DELETE` in create mode.
  - On entering the editor, always `GET` the draft first. If one exists, decide what to do based on
    `editing_test_id`:
    - `null` → it is a new-test draft. Offer to resume it.
    - matches the test being edited → resume as today.
    - belongs to a *different* test → warn before overwriting.
  - Never silently discard a draft. If the lecturer opens a fresh "Create New Test" while a draft
    exists, show an explicit choice: **Resume draft** / **Start blank (discards draft)**.
- Surface drafts in the test library (`#screen-host-tests`): render a distinct "Draft in progress"
  card at the top when `GET /api/drafts/{subject}` returns one, showing title, question count and
  last-saved time, with **Resume** and **Discard** buttons.
- Make failures visible and honest:
  - `POST /api/drafts/{subject}` must return a real error status (500/503) when the write genuinely
    failed. Keep autosave non-fatal in the UI, but the status line must read "Draft NOT saved — <reason>"
    in red, never a success message.
  - `saveDraft()` in `app.js` must check `resp.ok === false` as well as the HTTP status.
  - `HybridTestRepository.save_draft()` must stop swallowing exceptions blindly. Log the exception,
    record it on the repo, and report in the response which backend actually stored the draft
    (`"storedIn": "supabase" | "local-file" | "memory"`). Show that in the draft status line, e.g.
    "Draft saved to Supabase 14:32" vs "Draft saved locally — will be lost if the server redeploys".
- Add a **browser-side `localStorage` mirror** as a safety net. Write the draft to `localStorage` on
  every autosave keyed by subject code. On opening the editor, if the localStorage copy is newer than
  the server copy, offer to restore it. This makes drafts survive server restarts, Supabase outages and
  Render's ephemeral disk entirely — it is the single highest-value part of this fix.
- Clear `draftSaveTimer` whenever the editor screen is left, and flush a pending save synchronously
  first.
- Add a `beforeunload` handler warning about unsaved changes when `draftDirty` is true.
- Verify `quiz_test_drafts` actually exists in the live Supabase project. Add a
  `GET /api/diagnostics` endpoint (lecturer-auth required) that probes each table
  (`quiz_lecturers`, `quiz_tests`, `quiz_test_drafts`, `quiz_subjects`) and reports reachable/missing
  plus the error text, so this class of problem is diagnosable in future instead of silent.

### Tests that must pass
- Save a draft in create mode → navigate away → re-enter the editor → the draft is offered and restores
  with all questions intact.
- Same, but with Supabase unreachable → the UI clearly states the draft was stored locally only.
- Same, but with the server restarted and no persistent storage → the localStorage copy is offered.
- A failed draft write never displays a success message.

### Alternative the user raised
The user asked whether it would be simpler to **remove Save Draft entirely** and rely on the working
Save button. Do **not** do this by default — the fix above is small and drafts matter for long quizzes.
But once implemented, if you find the resume flow is still confusing, say so explicitly in your summary
and propose the simpler alternative rather than shipping a half-working feature.

---

## 3. Bug fixes

Verify each against the source before fixing — do not fix by rote.

### Correctness / race conditions
1. **Double question advance.** `advance_to_next()` (`server.py` ~2658) can run concurrently if the host
   clicks "Next Question" twice quickly, or if it races the `auto_reveal()` timer. Each call does
   `room.current_q += 1`, so questions get skipped. Add an `asyncio.Lock` per room, or a
   `room.advancing` guard plus an expected-question-index check.
2. **Double reveal.** `maybe_finish_question_early()` (~2097) can be entered by two concurrent answer
   handlers, both passing the `phase == "question"` check, causing `mark_unanswered_players()` to run
   twice and append duplicate answer records. Guard with the same lock.
3. **`start_game` blocks the host socket for 20 seconds.** The `await asyncio.sleep(20)` for the entry
   code (~2319) sits inside the WebSocket receive loop, so the host cannot pause or cancel during the
   countdown and queued messages stall. Move the countdown into a background task.
4. **Dict mutated during async iteration.** `get_subjects()` (~1446) iterates `SUBJECTS.items()` while
   awaiting; a concurrent `DELETE /api/subjects` pops from `SUBJECTS` and raises
   `RuntimeError: dictionary changed size during iteration`. Iterate over a snapshot (`list(...)`).
5. **N+1 queries.** The same endpoint issues one `list_tests` Supabase round-trip per subject. Replace
   with a single query returning counts grouped by `subject_code`.
6. **`datetime.utcnow()` is deprecated** in Python 3.12+ (this deploys on 3.13). Replace all uses with
   `datetime.now(timezone.utc)`. Check the resulting ISO strings still parse correctly on the client.
7. **Password hashing blocks the event loop.** `hash_password` / `verify_password` run 260k PBKDF2
   rounds synchronously (~100–200 ms), stalling every other request including live games. Wrap in
   `asyncio.to_thread` / `run_in_executor`.
8. **Unvalidated answer choice.** The `answer` WebSocket handler (~2513) takes `msg.get("choice", -1)`
   with no type or range check. Validate it is an `int` in `0..3` and reject otherwise.
9. **Timer percentage uses a hardcoded constant.** `app.js` sets `hostTimeLeft`/`timeLeft` from
   `msg.timeLimit` but computes the progress-bar percentage against the module constant `TIME_PER_Q`.
   These silently disagree the moment the time limit becomes configurable (Phase 5). Store the
   server-provided limit and use it for both.

### HTML / UI
10. **Duplicate element ID.** `id="btn-pause-game"` appears twice in `index.html` (lines ~397 and ~422).
    Invalid HTML; `$('#btn-pause-game')` only ever finds the first. Convert to a class
    (`.btn-pause-game`) and update `app.js`.
11. **Mislabelled button.** In `renderHostTestCards()` (`app.js` ~1417) a built-in test gets the label
    "Edit Test" but the `test-duplicate-btn` class, so it reads Edit but performs Duplicate. Label it
    "Duplicate Test" (or "Copy & Edit").
12. **Player can't go back.** `showPlayerJoinScreen()` (~520) always sets `#btn-back-subject.hidden = true`,
    so a student who picks the wrong subject is stuck. Show it when the player arrived via subject
    selection rather than a QR token.
13. **Confusing rejoin state.** In `handlePlayerMessage` `case 'joined'` (~713), a player who reconnects
    mid-question having already answered is dumped on the lobby screen. Show a proper
    "Answer submitted — waiting for other students" screen instead.
14. **Unguarded JSON parse.** `ws.onmessage` calls `JSON.parse` with no try/catch; one malformed frame
    kills the handler. Wrap it.
15. **Leaked intervals.** The countdown intervals in `playerGetReady()` and `hostShowReveal()` are never
    cleared if the screen changes early. Track and clear them in `showScreen()`.

### Robustness
16. `GameRoom.__init__` raises `KeyError` if the subject code is missing from `SUBJECTS`. Fail gracefully.
17. Add WebSocket message size limits and a per-room player cap (e.g. 300) to prevent trivial resource
    exhaustion.

---

## 4. Security and privacy

The user has confirmed that **automatic download of the results spreadsheet when a quiz ends is
intended behaviour and must be preserved.** The host browser is already authenticated, so its `fetch`
will carry the session cookie — adding auth to the endpoint does not break the auto-download. Verify
this explicitly after the change.

1. **`GET /api/stats/{subject_code}` has no authentication** (`server.py` ~1702). Anyone who knows a
   subject code can download a spreadsheet of **student names, student numbers and per-question
   results**. This is a personal-data exposure and, for a South African institution, a POPIA problem.
   Require `require_lecturer` on this endpoint. Confirm the end-of-game auto-download still works.
2. **Client-supplied identity.** `visitorId` comes from a query parameter (`app.js` ~25), so a student
   can supply someone else's ID and hijack their session or score. Issue a server-signed visitor token
   (HMAC, same pattern as the lecturer session) and validate it on `player_join`.
3. **Open lecturer signup.** Anyone reaching the URL can create a lecturer account and then create
   subjects and tests. Add an environment-variable gate, checked in this order:
   - `ALLOWED_EMAIL_DOMAINS` — comma-separated (e.g. the university domain). Colleagues self-serve with
     their work email, no admin involvement. **This is the preferred mechanism.**
   - `SIGNUP_INVITE_CODE` — a shared code, accepted as an alternative or in addition.
   - **If neither variable is set, signup stays open exactly as it is today.** This is important: the
     change must not lock anyone out of a running deployment before the owner has configured it.
   Add a clear error message on the signup screen when an email is rejected, naming the accepted domain.
4. **Weak session secret fallback.** `_session_secret()` falls back to the literal
   `"engineering-quiz-dev-secret"`. Refuse to start in production (i.e. when `SUPABASE_URL` is set)
   unless `APP_SESSION_SECRET` is explicitly configured. Add it to `render.yaml` as a `sync: false` var.
   **Document prominently** that on first deploy the owner should set `APP_SESSION_SECRET` to the
   *current* `SUPABASE_SERVICE_ROLE_KEY` value, because that is what sessions are already signed with —
   doing so means no lecturer is signed out by this change. They can rotate it to a fresh random value
   later at a convenient moment.
5. **PostgREST filter injection.** Values such as `test_id` and `subject_code` are interpolated straight
   into filter strings (`f"eq.{test_id}"`). Validate `test_id` is a UUID or the `local:` form, and
   `subject_code` against `SUBJECT_CODE_PATTERN`, before building any query.
6. **CORS.** `ALLOWED_ORIGINS` defaults to `*`. Since the app is served same-origin, default to the
   deployment origin instead and keep `*` opt-in only.
7. **Rate limiting.** Currently only on signup/login. Add limits to test create/update and draft save.
   **Be careful here:** draft autosave fires 1.5 s after typing stops, so a lecturer typing a long quiz
   legitimately generates many draft writes per minute. Set the draft limit generously (≥60/minute) or
   exempt drafts entirely. A tight limit here would break the very feature Phase 2 is fixing.
8. **Information leakage.** `/api/health` and `/api/storage-status` return raw `supabaseError` text to
   anonymous callers. Return a generic status publicly and the detail only to authenticated lecturers.
9. Delete the stale claim in `README_RENDER.txt` about a lecturer passcode stored in `app.js` — that
   mechanism no longer exists and the note is misleading.

### Impact on lecturers signing in — must be preserved
The owner has asked specifically how this affects logins. Verify each of these before you finish:

- **Existing accounts and passwords are untouched.** No change here re-hashes or invalidates any
  password. Nothing in this phase requires anyone to reset a password.
- **Nobody should be signed out** provided `APP_SESSION_SECRET` is seeded with the current service role
  key value as described in item 4. If you cannot guarantee that, say so explicitly and warn that
  lecturers will need to sign in once after deployment.
- **Other lecturers must still be able to create their own accounts** without the owner doing manual
  work. That is why the domain allowlist is preferred over admin approval. Do not implement a flow that
  requires the owner to provision accounts by hand.
- **Test ownership is unchanged.** Lecturers see all tests for a subject and can edit only their own
  (`canEdit`). Do not tighten this into per-lecturer isolation — shared visibility is intentional.
- **The end-of-quiz automatic download must still work** once `/api/stats/` requires auth. Test it.
- Deploy the signed-visitor-token change (item 2) **between classes**, not during one: students
  connected at that moment will be disconnected and have to rejoin.

---

## 5. Word document import (the requested new feature)

**Yes, this is entirely achievable.** Build it as follows.

### Backend
- Add `python-docx` to `requirements.txt`.
- New endpoint `POST /api/import/questions` — lecturer-auth required, `multipart/form-data`, accepts a
  single `.docx`. Limit to 5 MB and 200 questions. It **parses and returns JSON only** — it must never
  write a test directly. The lecturer always reviews before saving.
- Response shape:
  ```json
  {
    "questions": [ { "q": "...", "options": ["A","B","C","D"], "correct": 1, "explanation": "..." } ],
    "warnings": [ { "index": 3, "message": "No correct answer marked — defaulted to A" } ],
    "meta": { "title": "...", "parsed": 12, "skipped": 1 }
  }
  ```

### Parsing rules — numbered-text format
The lecturer's preferred layout:

```
1. What is the SI unit of force?
A) Newton
B) Joule
C) Watt
D) Pascal
Answer: A
Explanation: Force is measured in newtons.
```

Be deliberately forgiving:
- Question numbering: `1.` `1)` `Q1.` `Question 1:` — **and none at all.** This matters: Word's
  automatic list numbering is *not present in the paragraph text* that `python-docx` extracts. You
  must therefore also group by blank lines / option-marker detection, and read `pPr/numPr` to detect
  auto-numbered paragraphs. Test with a document that uses Word's automatic numbering, not just typed
  numbers — this is the single most common way a naive parser fails.
- Option markers: `A)` `A.` `(A)` `a)` `a.` plus Word bullet/list paragraphs with no marker at all.
- Correct answer: `Answer: B`, `Ans: B`, `Correct: B`, `Answer: Newton` (match by option text as well
  as by letter), or an asterisk `*` prefix/suffix on the correct option.
- Explanation: `Explanation:`, `Rationale:`, `Feedback:`, `Why:` — optional.
- Require exactly 4 options to match the existing `QuestionPayload` model. If a question yields 2 or 3,
  emit a warning and skip it rather than silently padding.
- Preserve Unicode symbols correctly (θ, Σ, °, µ, ±, ×, subscripts/superscripts) — the user's questions
  contain these. Normalise Word's smart quotes and en/em dashes. Do **not** strip non-ASCII.
- Word equation objects (OMML) are **not** returned by `python-docx` text extraction and will be
  silently lost. Detect `<m:oMath>` elements in the paragraph XML and emit an explicit per-question
  warning: "This question contains a Word equation that could not be imported — please retype it as
  text." The user reports only occasional symbols, so full OMML→LaTeX conversion is out of scope, but
  the warning is mandatory. Never drop content silently.
- Also parse a **table layout** if the document contains a table whose header row matches
  Question / A / B / C / D / Answer / Explanation. This is far more reliable than prose parsing, so
  prefer it when present.

### Downloadable template (explicitly requested)
- Add `GET /api/import/template` returning a generated `.docx` built with `python-docx` at request time
  (do not commit a binary to the repo). It must contain:
  - A short instructions section at the top showing the exact accepted format.
  - 3 worked example questions in the numbered-text format, using realistic mechanics content
    including a symbol or two (e.g. θ, Σ) so the encoding path is exercised.
  - An example table-format section.
  - A note that Word equation objects will not import.
- Link it prominently from the import UI: **"Download question template (.docx)"**.

### Front end
- On `#screen-host-create-test`, add an import panel above the question list:
  - "Import questions from Word" button plus a drag-and-drop zone.
  - "Download question template (.docx)" link.
- After upload, show a **review modal** before anything touches the editor:
  - parsed question count, each question with its detected correct answer, and all warnings inline
  - actions: **Append to current test** / **Replace all questions** / **Cancel**
- Imported questions land in the normal editors and are fully editable — the import is a shortcut, not
  a separate path.
- Mark the draft dirty after import so the work is autosaved.

### Tests
Include fixture `.docx` files covering: typed numbering, Word auto-numbering, table layout, asterisk-
marked answers, answer-by-text, a question with only 3 options, a question containing an equation
object, and Unicode symbols.

---

## 6. Configurable question timing (explicitly requested — treat as a core feature, not an extra)

The owner wants to set the time allowed per question when building a quiz: longer for hard calculation
questions, shorter for quick recall. Currently `TIME_PER_Q = 30` is a server constant.

### Data model
- Add `default_time_limit integer not null default 30` to `quiz_tests` (and to `quiz_test_drafts`).
- Add an **optional** per-question `time_limit` inside the existing `questions` JSONB. No schema change
  needed for that — but add it to `QuestionPayload` and `DraftQuestionPayload` as
  `time_limit: int | None = Field(default=None, ge=5, le=300)`.
- Resolution order at runtime: question-level → test-level → `TIME_PER_Q` (which becomes a fallback
  default, not a hard constant).
- Bounds: 5–300 seconds. Reject anything outside that with a clear validation message.
- **Backward compatibility is mandatory.** Existing tests have no `default_time_limit` and no
  per-question value; they must continue to run at exactly 30 seconds with no migration.

### Server changes
- `GameRoom` gains `default_time_limit`; `set_active_test()` reads it from the test row.
- `send_question()` resolves the limit for the current question and sends it in the existing
  `timeLimit` field to both host and players. The `_timer()` loop must count against the resolved
  limit, not the constant.
- Scoring: `time_fraction = min(answer_time / resolved_limit, 1.0)` — so a 90-second question still
  awards the full 1000 points for a fast answer. Do not let the constant leak into the points maths.
- `build_joined_payload()` must compute `remaining` against the resolved limit so mid-question
  reconnects show the right countdown.
- Consider making the 5-second reveal pause configurable too (`reveal_seconds`, default 5) — useful
  when explanations are long.

### Editor UI (`#screen-host-create-test`)
- A test-level control near the title: **"Default time per question"** with quick presets and a custom
  value: `10s (quick recall) · 30s (standard) · 60s · 90s · 120s (calculation) · Custom`.
- Per-question override inside each question card: a small select defaulting to
  **"Use test default (30s)"**, with the same presets plus custom. The label must update live when the
  test-level default changes, so it always shows the real inherited value.
- Show a running **"Estimated quiz length: 12 min"** total (sum of all question limits + 3 s ready +
  5 s reveal each). This is genuinely useful for fitting a quiz into a lecture slot.
- Include `time_limit` in `collectTestFormPayload()` **and** `collectDraftFormPayload()` so timing
  survives drafts. Missing it from the draft payload is an easy bug — test for it.
- The Word import (Phase 5) should accept an optional `Time: 60` line per question and map it to the
  per-question override.

### Client timer correctness
This depends on bug 3.9 being fixed first. `app.js` currently sets the countdown from `msg.timeLimit`
but computes the progress-bar width against the module constant `TIME_PER_Q`. With variable limits a
90-second question would render a bar 300 % wide and a 10-second one would look nearly empty. Store the
server-provided limit per question and use it for both value and percentage, on the player and host
screens alike.

### Host controls
- Show the current question's limit on the host screen.
- Consider a **"+15 seconds"** button letting the lecturer extend the live question when the room is
  clearly still working. The pause mechanism already exists, so extending is a small addition and
  very useful in a real classroom.

### Tests
A test mixing 10 s, 30 s and 120 s questions must: award correct points at each limit, render correct
bar percentages, resume correctly on mid-question reconnect, and round-trip through a draft save.

---

## 7. Further improvements (phased — get 1–6 shipped and committed first)

### Phase 7a — high value, low risk
- **Persist game results — storage-conscious design.** `last_game_stats` lives only in process memory,
  so a Render restart or redeploy destroys the results of any quiz whose spreadsheet was not downloaded.

  The owner is on Supabase's free plan (500 MB database) and is concerned about space, and **prefers the
  automatic download to their PC as the primary record.** Respect that:
  - The auto-download at the end of a game stays the authoritative copy. Do not change it.
  - Store only a **lightweight** row per session in `quiz_game_results`: subject, test id and title,
    session name, timestamp, player count, and the per-student answer array. Do **not** duplicate the
    full question text — reference the test id instead.
  - Add **automatic pruning**, configurable via `RESULTS_RETENTION` (default: keep the most recent 20
    sessions per subject, delete older). Run the prune after each insert.
  - Add `PERSIST_RESULTS` (default `true`) so the whole feature can be switched off with one
    environment variable if space ever becomes a concern.
  - Add a storage-usage line to the lecturer's library screen: number of stored sessions and their
    approximate size, so the owner can see the cost rather than guess at it.

  Size reality, so this is not decided on a hunch: a 40-student, 20-question session is roughly 50 KB
  of JSONB — about **0.01 %** of the 500 MB free-plan database. At 150 sessions a year that is ~7.5 MB
  per year, and with 20-session pruning it never exceeds ~1 MB. Tests and lecturer accounts are
  negligible. State the measured figure in `CHANGES.md` after implementing.
- **Answer distribution on the reveal screen.** The server already holds every choice in
  `answers_this_round`. Show the host a bar chart of how many students picked each option — this is the
  single most useful teaching signal in a live quiz and is nearly free to add.
- **Post-game student review.** Let students see which questions they got wrong, with the correct answer
  and the explanation, after the final leaderboard.
- **Guard against the free-plan project pause.** Supabase free projects are paused after 7 days of
  inactivity, which would take the app down mid-semester-break and return confusing errors. Detect the
  paused/unreachable state and show the lecturer a clear message ("The question database is asleep —
  open your Supabase dashboard to resume it") rather than a generic failure.

### Phase 7b — content features
- **Images per question.** Free-body diagrams, beam loading, mechanisms — near-essential for mechanics.
  Store in Supabase Storage; add an upload control in the editor and render above the question text on
  both host and player screens.
- **More question types.** True/False, multiple-correct, and **numeric answer with a tolerance**
  (e.g. "45.2 ± 0.5 kN"). Numeric entry is a natural fit for engineering and removes the guess-from-four
  problem.
- **Question bank + random selection.** Store a pool per subject and draw N at random per session, so
  the same class can play twice without repeating.
- **Export a test to Word/PDF** for printed tutorials — the inverse of Phase 5, reusing the same format.
- **MathJax/LaTeX rendering.** Deferred: the user reports mostly plain text with a few symbols. Revisit
  only if equation-heavy questions become common.

### Phase 7c — classroom operations
- Class list upload (CSV of student numbers) to validate joins and catch typos in student numbers,
  which currently corrupt the results spreadsheet silently.
- Team mode (students grouped, scores aggregated).
- Practice mode: a self-paced version with no host, for revision between lectures.
- Accessibility pass: keyboard answer selection (1–4), verified colour contrast, respect
  `prefers-reduced-motion`. The coloured shapes already help colour-blind students — keep that.
- Cross-session analytics: which questions are consistently answered worst, so the lecturer knows what
  to reteach.

---

## 8. Constraints

- **Do not break the existing deployment.** Render free tier, single worker, `uvicorn server:app`.
- Keep the no-build-step front end unless you propose a build step explicitly and get agreement first.
- All Supabase schema changes go into `supabase_schema.sql` and must stay **idempotent and re-runnable** —
  the lecturer applies it by pasting into the Supabase SQL editor.
- Preserve backward compatibility with existing saved tests and lecturer accounts. Existing data must
  not require migration by hand.
- Preserve the automatic results download at the end of a game.
- Never log passwords, session tokens or the service role key.
- Keep student personal data (names, student numbers) out of any endpoint that is not lecturer-authenticated.

## 9. Deliverables

1. Passing test suite (Phase 1) plus tests for every fix and feature.
2. Updated `supabase_schema.sql` if any tables changed, with a note on what must be re-run.
3. Updated `requirements.txt` and `render.yaml` (new environment variables).
4. `README_DEV.md` (local dev + testing) and an updated `README_RENDER.txt` (with the stale passcode
   note removed and any new env vars documented).
5. A `CHANGES.md` summarising, per phase: what was fixed, what was added, anything deliberately not done
   and why.
6. Separate commits per phase.

## 10. Report back

When finished, state plainly:
- the confirmed root cause of the draft bug and whether your fix resolves it end to end
- any bug in section 3 you could not reproduce (do not "fix" phantom bugs)
- what the Word import will and will not handle, with the equation limitation stated in plain language
- which Phase 7 items you completed versus deferred
- the measured storage cost per stored game session, in KB
