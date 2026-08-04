"""Phase 7a: stored game results, answer distribution and post-game review."""

import json

import pytest
from conftest import SUBJECT, load_server, make_question, make_test_payload, signup, ws_url
from fastapi.testclient import TestClient
from test_game_flow import wait_for


@pytest.fixture()
def authed(client):
    signup(client)
    return client


def two_question_test(client, title="Results test"):
    payload = make_test_payload(
        title=title,
        questions=[make_question(1, correct=0), make_question(2, correct=1)],
    )
    return client.post(f"/api/tests/{SUBJECT}", json=payload).json()["test"]["id"]


def play_game(client, test_id, session_name="Tutorial 1", players=(("Ada", "1", 0), ("Ben", "2", 1))):
    """Play a full two-question game and return the host's last messages."""
    sockets = []
    with client.websocket_connect(ws_url(client, f"host-{session_name}")) as host:
        host.send_json({"action": "host_join", "subject": SUBJECT, "testId": test_id, "sessionName": session_name})
        joined = wait_for(host, "host_joined")
        if joined["phase"] != "lobby":
            # Replaying the same test needs an explicit reset, exactly as the
            # lecturer's "New Game" button does. That issues a fresh session
            # token, which students pick up by rescanning the QR code.
            host.send_json({"action": "reset_game"})
            joined = wait_for(host, "host_joined")
        token = joined.get("sessionToken") or ""
        try:
            for name, number, _ in players:
                ws = client.websocket_connect(ws_url(client, f"p-{session_name}-{name}")).__enter__()
                sockets.append(ws)
                ws.send_json({
                    "action": "player_join", "name": name, "studentNumber": number,
                    "subject": SUBJECT, "token": token,
                })
                wait_for(ws, "joined")

            host.send_json({"action": "start_game", "shuffle": False, "useCode": False})
            reveals = []
            for _ in range(2):
                for ws, (_, _, choice) in zip(sockets, players, strict=False):
                    wait_for(ws, "question")
                    ws.send_json({"action": "answer", "choice": choice})
                    wait_for(ws, "answer_result")
                reveals.append(wait_for(host, "reveal")) if _ == 0 else None
            final = wait_for(host, "final")
            reviews = [wait_for(ws, "review") for ws in sockets]
            return {"final": final, "reveals": reveals, "reviews": reviews}
        finally:
            for ws in sockets:
                try:
                    ws.__exit__(None, None, None)
                except Exception:
                    pass


# ── Persisted results ────────────────────────────────────────────────────────

def test_a_finished_game_is_stored(authed):
    test_id = two_question_test(authed)
    play_game(authed, test_id, session_name="Tut1")

    body = authed.get(f"/api/results/{SUBJECT}").json()
    assert len(body["results"]) == 1
    row = body["results"][0]
    assert row["session_name"] == "Tut1"
    assert row["player_count"] == 2
    assert row["question_count"] == 2
    # The listing must not carry personal data.
    assert "players" not in row


def test_stored_results_survive_a_restart(authed, restart):
    test_id = two_question_test(authed)
    play_game(authed, test_id, session_name="Tut1")

    module = restart()
    with TestClient(module.app) as fresh:
        fresh.post("/api/lecturer/login", json={"email": "lecturer@example.com", "password": "test-password-1"})
        results = fresh.get(f"/api/results/{SUBJECT}").json()["results"]
        assert len(results) == 1, "results were lost across a restart"
        assert results[0]["session_name"] == "Tut1"


def test_a_stored_result_holds_the_per_student_answers_but_not_the_questions(authed):
    test_id = two_question_test(authed)
    play_game(authed, test_id, session_name="Tut1")

    listed = authed.get(f"/api/results/{SUBJECT}").json()["results"][0]
    row = authed.get(f"/api/results/{SUBJECT}/{listed['id']}").json()

    assert {p["name"] for p in row["players"]} == {"Ada", "Ben"}
    assert row["test_id"] == test_id
    # Question text is referenced by test_id, never duplicated.
    assert "questions" not in row
    for player in row["players"]:
        assert len(player["answers"]) == 2
        assert set(player["answers"][0]) == {"q", "choice", "correct", "points", "time"}


def test_results_are_pruned_to_the_retention_limit(monkeypatch, store_path):
    module = load_server(monkeypatch, store_path, RESULTS_RETENTION="2")
    with TestClient(module.app) as client:
        signup(client)
        test_id = two_question_test(client)
        for i in range(4):
            play_game(client, test_id, session_name=f"S{i}")

        results = client.get(f"/api/results/{SUBJECT}").json()["results"]
        assert len(results) == 2, "old sessions should be pruned after each insert"
        assert {r["session_name"] for r in results} == {"S2", "S3"}


def test_persistence_can_be_switched_off(monkeypatch, store_path):
    module = load_server(monkeypatch, store_path, PERSIST_RESULTS="false")
    with TestClient(module.app) as client:
        signup(client)
        test_id = two_question_test(client)
        play_game(client, test_id, session_name="Off")
        body = client.get(f"/api/results/{SUBJECT}").json()
        assert body["results"] == []
        assert body["storage"]["enabled"] is False


def test_results_endpoints_require_lecturer_auth(client):
    assert client.get(f"/api/results/{SUBJECT}").status_code == 401
    assert client.get(f"/api/results/{SUBJECT}/anything").status_code == 401


def test_the_end_of_game_download_still_works_alongside_storage(authed):
    """Storing results must not disturb the authoritative download."""
    test_id = two_question_test(authed)
    play_game(authed, test_id, session_name="Tut1")
    resp = authed.get(f"/api/stats/{SUBJECT}")
    assert resp.status_code == 200
    assert len(resp.content) > 1000


# ── Measured storage cost (reported in CHANGES.md) ───────────────────────────

def test_measured_storage_cost_per_session(authed, server_module, capsys):
    """Measure a realistic 40-student, 20-question session."""
    stats = {
        "subject_code": SUBJECT,
        "subject_name": "Mechanics",
        "test_id": "3f2504e0-4f89-11d3-9a0c-0305e82c3301",
        "test_title": "Chapter 3 Forces Quiz",
        "session_name": "Monday 9am",
        "timestamp": "2026-08-04T09:00:00+00:00",
        "questions": [{"q": "x" * 120, "options": ["a" * 40] * 4, "correct": 0, "explanation": "y" * 150}] * 20,
        "players": {
            f"student-{i}": {
                "name": f"Student Number {i}",
                "student_number": f"2210{i:05d}",
                "score": 12345,
                "answers": [
                    {"q": q, "choice": q % 4, "correct": q % 2 == 0, "points": 780, "time": 7.25}
                    for q in range(20)
                ],
            }
            for i in range(40)
        },
    }
    row = server_module.repo._result_row(stats, None)
    size_kb = len(json.dumps(row, ensure_ascii=False).encode("utf-8")) / 1024

    # Sanity bound, and the number quoted in CHANGES.md.
    assert size_kb < 80, f"a stored session is {size_kb:.1f} KB, larger than expected"
    print(f"\nMeasured: one 40-student, 20-question session = {size_kb:.1f} KB "
          f"({size_kb / (500 * 1024) * 100:.4f}% of the 500 MB free plan)")


def test_storage_usage_is_reported_to_the_lecturer(authed):
    test_id = two_question_test(authed)
    play_game(authed, test_id, session_name="Tut1")
    storage = authed.get(f"/api/results/{SUBJECT}").json()["storage"]
    assert storage["enabled"] is True
    assert storage["sessions"] == 1
    assert storage["retention"] == 20
    assert storage["approxKb"] > 0


# ── Answer distribution ──────────────────────────────────────────────────────

def test_reveal_carries_the_answer_distribution(authed):
    test_id = two_question_test(authed)
    with authed.websocket_connect(ws_url(authed, "host-d")) as host, \
         authed.websocket_connect(ws_url(authed, "p-d1")) as a, \
         authed.websocket_connect(ws_url(authed, "p-d2")) as b:
        host.send_json({"action": "host_join", "subject": SUBJECT, "testId": test_id})
        wait_for(host, "host_joined")
        for ws, (name, number) in zip((a, b), (("Ada", "1"), ("Ben", "2")), strict=False):
            ws.send_json({"action": "player_join", "name": name, "studentNumber": number, "subject": SUBJECT})
            wait_for(ws, "joined")
        host.send_json({"action": "start_game", "shuffle": False, "useCode": False})

        wait_for(a, "question")
        wait_for(b, "question")
        a.send_json({"action": "answer", "choice": 0})     # correct
        wait_for(a, "answer_result")
        b.send_json({"action": "answer", "choice": 2})     # a distractor
        wait_for(b, "answer_result")

        reveal = wait_for(host, "reveal")
        assert reveal["distribution"] == [1, 0, 1, 0]
        assert reveal["answered"] == 2
        assert reveal["options"] == ["Newton", "Joule", "Watt", "Pascal"]


# ── Post-game student review ─────────────────────────────────────────────────

def test_students_receive_their_own_answers_for_review(authed):
    test_id = two_question_test(authed)
    outcome = play_game(authed, test_id, session_name="Tut1", players=(("Ada", "1", 0), ("Ben", "2", 1)))

    # Ada answered 0 on both: right on Q1, wrong on Q2 (correct is 1).
    ada = outcome["reviews"][0]["questions"]
    assert len(ada) == 2
    assert ada[0]["wasCorrect"] is True
    assert ada[1]["wasCorrect"] is False
    assert ada[1]["correct"] == 1
    assert ada[1]["yourChoice"] == 0
    assert ada[1]["explanation"]

    # Ben answered 1 on both: wrong on Q1, right on Q2.
    ben = outcome["reviews"][1]["questions"]
    assert ben[0]["wasCorrect"] is False
    assert ben[1]["wasCorrect"] is True


# ── Supabase free-plan pause detection ───────────────────────────────────────

def test_a_paused_supabase_project_is_reported_in_plain_language(authed, server_module):
    repo = server_module.repo
    repo.supabase_configured = True
    repo.supabase_error = "[Errno 11001] getaddrinfo failed"

    status = authed.get("/api/storage-status").json()
    assert status["asleep"] is True

    repo.supabase_error = "PGRST205 could not find the table 'quiz_tests'"
    assert authed.get("/api/storage-status").json()["asleep"] is False


def test_sleep_state_is_visible_without_leaking_error_detail(authed, server_module):
    server_module.repo.supabase_configured = True
    server_module.repo.supabase_error = "connection refused to db.abcxyz.supabase.co"
    authed.cookies.clear()
    public = authed.get("/api/storage-status").json()
    assert public["asleep"] is True
    assert "abcxyz" not in json.dumps(public)
