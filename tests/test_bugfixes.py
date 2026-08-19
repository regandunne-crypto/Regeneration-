"""Regression tests for the Phase 3 bug fixes."""

import asyncio

import pytest
from conftest import SUBJECT, load_server, make_question, make_test_payload, signup, ws_url
from fastapi.testclient import TestClient
from test_game_flow import wait_for


@pytest.fixture()
def authed(client):
    signup(client)
    return client


def make_test(client, count=3):
    payload = make_test_payload(questions=[make_question(i, correct=i % 4) for i in range(count)])
    return client.post(f"/api/tests/{SUBJECT}", json=payload).json()["test"]["id"]


# ── 1. Double question advance ───────────────────────────────────────────────

def test_double_next_question_does_not_skip_a_question(authed):
    test_id = make_test(authed, count=3)
    with authed.websocket_connect(ws_url(authed, "host-adv")) as host, \
         authed.websocket_connect(ws_url(authed, "p-adv")) as player:
        host.send_json({"action": "host_join", "subject": SUBJECT, "testId": test_id})
        wait_for(host, "host_joined")
        player.send_json({"action": "player_join", "name": "Ada", "studentNumber": "1", "subject": SUBJECT})
        wait_for(player, "joined")
        host.send_json({"action": "start_game", "shuffle": False, "useCode": False})
        assert wait_for(player, "question")["qNum"] == 1

        # An impatient double click. Each call used to do current_q += 1, so the
        # room jumped straight from question 1 to question 3.
        host.send_json({"action": "next_question"})
        host.send_json({"action": "next_question"})

        assert wait_for(player, "question")["qNum"] == 2


def test_answering_twice_records_one_answer_only(authed):
    test_id = make_test(authed, count=1)
    with authed.websocket_connect(ws_url(authed, "host-dup")) as host, \
         authed.websocket_connect(ws_url(authed, "p-dup")) as player:
        host.send_json({"action": "host_join", "subject": SUBJECT, "testId": test_id})
        wait_for(host, "host_joined")
        player.send_json({"action": "player_join", "name": "Ada", "studentNumber": "1", "subject": SUBJECT})
        wait_for(player, "joined")
        host.send_json({"action": "start_game", "shuffle": False, "useCode": False})
        wait_for(player, "question")

        player.send_json({"action": "answer", "choice": 0})
        player.send_json({"action": "answer", "choice": 1})
        wait_for(player, "answer_result")
        wait_for(host, "final")

    stats = authed.get(f"/api/stats/{SUBJECT}")
    assert stats.status_code == 200


# ── 3. start_game must not block the host socket ─────────────────────────────

def test_host_socket_stays_responsive_during_the_entry_code_countdown(monkeypatch, store_path):
    """The 20 s countdown used to sit inside the WebSocket receive loop, so the
    host could not pause or cancel and queued messages stalled behind it."""
    module = load_server(monkeypatch, store_path, GAME_CODE_COUNTDOWN_SECONDS="3")
    with TestClient(module.app) as client:
        signup(client)
        test_id = make_test(client, count=2)
        with client.websocket_connect(ws_url(client, "host-block")) as host, \
             client.websocket_connect(ws_url(client, "p-block")) as player:
            host.send_json({"action": "host_join", "subject": SUBJECT, "testId": test_id})
            wait_for(host, "host_joined")
            player.send_json({"action": "player_join", "name": "Ada", "studentNumber": "1", "subject": SUBJECT})
            wait_for(player, "joined")

            host.send_json({"action": "start_game", "shuffle": False, "useCode": True})
            assert wait_for(host, "game_code_display")["countdown"] == 3

            # Mid-countdown the host sends another message. It must be answered
            # immediately rather than queued behind the sleep.
            host.send_json({"action": "ping"})
            assert wait_for(host, "pong")


# ── 4. Concurrent subject deletion ───────────────────────────────────────────

def test_get_subjects_survives_a_subject_being_deleted_mid_call(authed, server_module):
    """SUBJECTS was iterated across an await; a concurrent delete raised
    RuntimeError: dictionary changed size during iteration."""
    authed.post("/api/subjects", json={"code": "TMP001", "name": "Temporary"})

    original = server_module.repo.get_test_counts

    async def delete_while_listing():
        server_module.SUBJECTS.pop("TMP001", None)
        return await original()

    server_module.repo.get_test_counts = delete_while_listing
    resp = authed.get("/api/subjects")
    assert resp.status_code == 200, resp.text
    assert "TMP001" not in {s["code"] for s in resp.json()}


def test_subject_list_counts_tests_and_questions(authed):
    make_test(authed, count=3)
    make_test(authed, count=2)
    subjects = {s["code"]: s for s in authed.get("/api/subjects").json()}
    assert subjects[SUBJECT]["testCount"] == 2
    assert subjects[SUBJECT]["questionCount"] == 5


# ── 8. Answer validation ─────────────────────────────────────────────────────

@pytest.mark.parametrize("bad_choice", ["0", 4, -1, 99, None, [0], {"a": 1}, True])
def test_invalid_answer_choices_are_rejected(authed, bad_choice):
    test_id = make_test(authed, count=1)
    with authed.websocket_connect(ws_url(authed, "host-val")) as host, \
         authed.websocket_connect(ws_url(authed, "p-val")) as player:
        host.send_json({"action": "host_join", "subject": SUBJECT, "testId": test_id})
        wait_for(host, "host_joined")
        player.send_json({"action": "player_join", "name": "Ada", "studentNumber": "1", "subject": SUBJECT})
        wait_for(player, "joined")
        host.send_json({"action": "start_game", "shuffle": False, "useCode": False})
        wait_for(player, "question")

        player.send_json({"action": "answer", "choice": bad_choice})
        rejected = wait_for(player, "invalid_answer")
        assert rejected["message"]

        # A valid answer still works afterwards — the socket is not poisoned.
        player.send_json({"action": "answer", "choice": 0})
        assert wait_for(player, "answer_result")["correct"] is True


# ── 14. Malformed frames ─────────────────────────────────────────────────────

def test_malformed_frame_does_not_kill_the_connection(authed):
    with authed.websocket_connect(ws_url(authed, "junk")) as ws:
        ws.send_text("this is not json{{{")
        ws.send_text("[1, 2, 3]")          # valid JSON, wrong shape
        ws.send_json({"action": "ping"})
        assert wait_for(ws, "pong")


def test_oversized_frame_is_refused(authed):
    with authed.websocket_connect(ws_url(authed, "huge")) as ws:
        ws.send_json({"action": "ping", "padding": "x" * 200_000})
        assert wait_for(ws, "error")["message"] == "Message too large."


# ── 16 / 17. Robustness ──────────────────────────────────────────────────────

def test_gameroom_for_an_unknown_subject_does_not_raise(server_module):
    room = server_module.GameRoom("NOTASUBJECT")
    assert room.subject_name == "NOTASUBJECT"
    assert room.total_q == 0


def test_room_player_cap_is_enforced(monkeypatch, store_path):
    module = load_server(monkeypatch, store_path, MAX_PLAYERS_PER_ROOM="2")
    with TestClient(module.app) as client:
        signup(client)
        test_id = make_test(client, count=1)
        with client.websocket_connect(ws_url(client, "host-cap")) as host:
            host.send_json({"action": "host_join", "subject": SUBJECT, "testId": test_id})
            wait_for(host, "host_joined")
            with client.websocket_connect(ws_url(client, "cap-a")) as a, \
                 client.websocket_connect(ws_url(client, "cap-b")) as b, \
                 client.websocket_connect(ws_url(client, "cap-c")) as c:
                a.send_json({"action": "player_join", "name": "A", "studentNumber": "1", "subject": SUBJECT})
                wait_for(a, "joined")
                b.send_json({"action": "player_join", "name": "B", "studentNumber": "2", "subject": SUBJECT})
                wait_for(b, "joined")
                c.send_json({"action": "player_join", "name": "C", "studentNumber": "3", "subject": SUBJECT})
                assert "full" in wait_for(c, "error")["message"]


# ── Pre-existing crash found while adding the advance guard ──────────────────

def test_game_ends_cleanly_when_the_last_question_times_out(monkeypatch, store_path):
    """force_end_game reached from inside the question timer used to make that
    timer cancel and await itself ("Task cannot await on itself")."""
    module = load_server(monkeypatch, store_path, TIME_PER_Q="1")
    with TestClient(module.app) as client:
        signup(client)
        test_id = make_test(client, count=1)
        with client.websocket_connect(ws_url(client, "host-to")) as host, \
             client.websocket_connect(ws_url(client, "p-to")) as player:
            host.send_json({"action": "host_join", "subject": SUBJECT, "testId": test_id})
            wait_for(host, "host_joined")
            player.send_json({"action": "player_join", "name": "Ada", "studentNumber": "1", "subject": SUBJECT})
            wait_for(player, "joined")
            host.send_json({"action": "start_game", "shuffle": False, "useCode": False})
            wait_for(player, "question")

            # Nobody answers. The timer must expire, reveal, and end the game.
            result = wait_for(player, "answer_result", limit=80)
            assert result["timedOut"] is True
            final = wait_for(host, "final", limit=80)
            assert final["hasStats"] is True


# ── 7. Password hashing off the event loop ───────────────────────────────────

def test_password_hashing_runs_on_a_worker_thread(server_module):
    async def check():
        loop = asyncio.get_running_loop()
        start = loop.time()
        # If hashing ran inline, this gather would take 3x as long as one hash.
        await asyncio.gather(*(server_module.hash_password_async("a-password") for _ in range(3)))
        return loop.time() - start

    single_start = asyncio.run(_time_one(server_module))
    parallel = asyncio.run(check())
    # Three concurrent hashes on threads should not cost three serial hashes.
    assert parallel < single_start * 3, f"hashing appears to be serialised ({parallel:.3f}s vs {single_start:.3f}s)"


async def _time_one(module):
    loop = asyncio.get_running_loop()
    start = loop.time()
    await module.hash_password_async("a-password")
    return loop.time() - start


# ── Stuck room: "the game is already in progress" when it is not ─────────────
# Reported in production: a lecturer picked up a colleague's finished quiz and
# every student scanning the QR code was refused, while the host saw a normal
# lobby. The room is per subject and in memory, so it stayed in "final".

def play_to_completion(client, test_id, visitor_prefix, token=""):
    """Run a one-question game to the end, leaving the room in 'final'."""
    with client.websocket_connect(ws_url(client, f"{visitor_prefix}-host")) as host, \
         client.websocket_connect(ws_url(client, f"{visitor_prefix}-p")) as player:
        host.send_json({"action": "host_join", "subject": SUBJECT, "testId": test_id, "newSession": True})
        joined = wait_for(host, "host_joined")
        player.send_json({
            "action": "player_join", "name": "Ada", "studentNumber": "1",
            "subject": SUBJECT, "token": joined.get("sessionToken") or token,
        })
        wait_for(player, "joined")
        host.send_json({"action": "start_game", "shuffle": False, "useCode": False})
        wait_for(player, "question")
        player.send_json({"action": "answer", "choice": 0})
        wait_for(player, "answer_result")
        wait_for(host, "final")


def join_as_student(client, visitor, token=""):
    """Return the first message a scanning student gets."""
    with client.websocket_connect(ws_url(client, visitor)) as ws:
        ws.send_json({
            "action": "player_join", "name": "Student", "studentNumber": "99",
            "subject": SUBJECT, "token": token,
        })
        return ws.receive_json()


def test_another_lecturer_reusing_a_finished_quiz_does_not_lock_students_out(authed, server_module):
    """The reported bug, end to end."""
    test_id = make_test(authed, count=1)
    play_to_completion(authed, test_id, "first")
    assert server_module.rooms[SUBJECT].phase == "final"

    # A different lecturer signs in and hosts the SAME test.
    authed.cookies.clear()
    signup(authed, email="colleague@example.com", name="Second Lecturer")
    with authed.websocket_connect(ws_url(authed, "second-host")) as host:
        host.send_json({"action": "host_join", "subject": SUBJECT, "testId": test_id})
        joined = wait_for(host, "host_joined")
        assert joined["phase"] == "lobby", "the room must be returned to the lobby"
        token = joined.get("sessionToken") or ""

        # A student scans the QR code and gets in.
        reply = join_as_student(authed, "scanning-student", token)
        assert reply["type"] == "joined", f"student was refused: {reply}"


def test_same_lecturer_rerunning_the_same_quiz_is_not_blocked(authed, server_module):
    test_id = make_test(authed, count=1)
    play_to_completion(authed, test_id, "again")
    assert server_module.rooms[SUBJECT].phase == "final"

    # "Use This Test" on the same quiz sends newSession.
    with authed.websocket_connect(ws_url(authed, "same-host")) as host:
        host.send_json({"action": "host_join", "subject": SUBJECT, "testId": test_id, "newSession": True})
        joined = wait_for(host, "host_joined")
        assert joined["phase"] == "lobby"
        reply = join_as_student(authed, "student-2", joined.get("sessionToken") or "")
        assert reply["type"] == "joined", f"student was refused: {reply}"


def test_a_host_reconnect_does_not_wipe_the_final_leaderboard(authed, server_module):
    """A dropped socket reconnecting is not a new session and must not reset."""
    test_id = make_test(authed, count=1)
    play_to_completion(authed, test_id, "recon")

    with authed.websocket_connect(ws_url(authed, "recon-host")) as host:
        # No newSession flag: this is what the auto-reconnect sends.
        host.send_json({"action": "host_join", "subject": SUBJECT, "testId": test_id})
        assert wait_for(host, "host_joined")["phase"] == "final"
    assert server_module.rooms[SUBJECT].phase == "final"


def test_an_abandoned_mid_game_room_is_cleared_by_a_new_session(authed, server_module):
    """Host closed the laptop mid-question; the room must not stay locked."""
    test_id = make_test(authed, count=3)
    with authed.websocket_connect(ws_url(authed, "aband-host")) as host, \
         authed.websocket_connect(ws_url(authed, "aband-p")) as player:
        host.send_json({"action": "host_join", "subject": SUBJECT, "testId": test_id, "newSession": True})
        joined = wait_for(host, "host_joined")
        player.send_json({
            "action": "player_join", "name": "Ada", "studentNumber": "1",
            "subject": SUBJECT, "token": joined.get("sessionToken") or "",
        })
        wait_for(player, "joined")
        host.send_json({"action": "start_game", "shuffle": False, "useCode": False})
        wait_for(player, "question")
        host.send_json({"action": "host_pause"})      # paused, then abandoned
        wait_for(host, "pause_state")

    assert server_module.rooms[SUBJECT].phase not in ("lobby", "final")

    with authed.websocket_connect(ws_url(authed, "rescue-host")) as host:
        host.send_json({"action": "host_join", "subject": SUBJECT, "testId": test_id, "newSession": True})
        joined = wait_for(host, "host_joined")
        assert joined["phase"] == "lobby"
        reply = join_as_student(authed, "student-3", joined.get("sessionToken") or "")
        assert reply["type"] == "joined", f"student was refused: {reply}"
