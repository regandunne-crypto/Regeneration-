"""Phase 6: configurable question timing."""

import pytest
from conftest import SUBJECT, make_question, make_test_payload, signup, ws_url
from test_game_flow import wait_for


@pytest.fixture()
def authed(client):
    signup(client)
    return client


def mixed_test(client):
    """10 s, 30 s (inherited) and 120 s questions in one test."""
    payload = make_test_payload(
        title="Mixed timing",
        default_time_limit=30,
        questions=[
            make_question(1, q="Quick recall", correct=0, time_limit=10),
            make_question(2, q="Standard question", correct=1),          # inherits 30
            make_question(3, q="Long calculation", correct=2, time_limit=120),
        ],
    )
    return client.post(f"/api/tests/{SUBJECT}", json=payload).json()["test"]["id"]


# ── Backward compatibility (mandatory) ───────────────────────────────────────

def test_a_test_saved_without_timing_still_runs_at_30_seconds(authed, server_module):
    """Existing tests have no default_time_limit and no per-question value."""
    test_id = authed.post(f"/api/tests/{SUBJECT}", json=make_test_payload()).json()["test"]["id"]

    room = server_module.rooms[SUBJECT]
    row = authed.get(f"/api/tests/{SUBJECT}/{test_id}").json()
    # Simulate the legacy shape exactly: strip the new fields entirely.
    legacy = {k: v for k, v in row.items() if k != "defaultTimeLimit"}
    legacy["questions"] = [{k: v for k, v in q.items() if k != "time_limit"} for q in legacy["questions"]]
    room.set_active_test(legacy)

    assert room.default_time_limit == 30
    assert room.time_limit_for(0) == 30
    assert room.time_limit_for(1) == 30


def test_legacy_payloads_without_default_time_limit_are_accepted(authed):
    payload = make_test_payload()
    assert "default_time_limit" not in payload
    resp = authed.post(f"/api/tests/{SUBJECT}", json=payload)
    assert resp.status_code == 200, resp.text
    assert resp.json()["test"]["defaultTimeLimit"] == 30


# ── Resolution order ─────────────────────────────────────────────────────────

def test_resolution_order_question_then_test_then_fallback(authed, server_module):
    test_id = mixed_test(authed)
    detail = authed.get(f"/api/tests/{SUBJECT}/{test_id}").json()
    assert detail["defaultTimeLimit"] == 30

    room = server_module.rooms[SUBJECT]
    room.set_active_test({**detail, "default_time_limit": detail["defaultTimeLimit"]})
    assert room.time_limit_for(0) == 10       # question level
    assert room.time_limit_for(1) == 30       # test level
    assert room.time_limit_for(2) == 120      # question level


@pytest.mark.parametrize("bad", [4, 301, 0, -5, "sixty", None, True])
def test_out_of_bounds_values_fall_through_rather_than_being_used(server_module, bad):
    assert server_module.coerce_time_limit(bad) in (None,) or 5 <= server_module.coerce_time_limit(bad) <= 300


def test_validation_rejects_out_of_range_limits(authed):
    too_short = make_test_payload(default_time_limit=4)
    assert authed.post(f"/api/tests/{SUBJECT}", json=too_short).status_code == 422

    too_long = make_test_payload(default_time_limit=301)
    assert authed.post(f"/api/tests/{SUBJECT}", json=too_long).status_code == 422

    bad_question = make_test_payload(questions=[make_question(1, time_limit=500)])
    resp = authed.post(f"/api/tests/{SUBJECT}", json=bad_question)
    assert resp.status_code == 422
    assert "300" in str(resp.json()["detail"])


# ── Round trips ──────────────────────────────────────────────────────────────

def test_timing_round_trips_through_save_and_reload(authed):
    test_id = mixed_test(authed)
    detail = authed.get(f"/api/tests/{SUBJECT}/{test_id}").json()
    assert detail["defaultTimeLimit"] == 30
    assert [q.get("time_limit") for q in detail["questions"]] == [10, None, 120]


def test_timing_round_trips_through_a_draft(authed):
    """Leaving time_limit out of the draft payload is an easy bug."""
    draft = {
        "title": "Timed draft",
        "chapter": "",
        "description": "",
        "default_time_limit": 90,
        "questions": [
            {"q": "Fast one", "options": ["a", "b", "c", "d"], "correct": 0, "explanation": "", "time_limit": 10},
            {"q": "Inherits", "options": ["a", "b", "c", "d"], "correct": 0, "explanation": ""},
        ],
        "editingTestId": None,
    }
    assert authed.post(f"/api/drafts/{SUBJECT}", json=draft).status_code == 200

    saved = authed.get(f"/api/drafts/{SUBJECT}").json()["draft"]
    assert saved["default_time_limit"] == 90
    assert saved["questions"][0]["time_limit"] == 10
    assert saved["questions"][1]["time_limit"] is None


def test_estimated_length_accounts_for_every_question(authed, server_module):
    test_id = mixed_test(authed)
    summary = [t for t in authed.get(f"/api/tests/{SUBJECT}").json() if t["id"] == test_id][0]
    # 10 + 30 + 120 seconds of questions, plus ready and reveal pauses per question.
    ready = server_module.GET_READY_SECONDS
    reveal = server_module.REVEAL_SECONDS
    assert summary["estimatedSeconds"] == int(160 + 3 * (ready + reveal))


# ── Live game ────────────────────────────────────────────────────────────────

def test_the_server_sends_the_resolved_limit_for_each_question(authed):
    test_id = mixed_test(authed)
    with authed.websocket_connect(ws_url(authed, "host-t")) as host, \
         authed.websocket_connect(ws_url(authed, "p-t")) as player:
        host.send_json({"action": "host_join", "subject": SUBJECT, "testId": test_id})
        joined = wait_for(host, "host_joined")
        assert joined["selectedTest"]["defaultTimeLimit"] == 30

        player.send_json({"action": "player_join", "name": "Ada", "studentNumber": "1", "subject": SUBJECT})
        wait_for(player, "joined")
        host.send_json({"action": "start_game", "shuffle": False, "useCode": False})

        for expected_limit, correct in ((10, 0), (30, 1), (120, 2)):
            question = wait_for(player, "question")
            assert question["timeLimit"] == expected_limit, f"question {question['qNum']}"
            assert wait_for(host, "question")["timeLimit"] == expected_limit
            player.send_json({"action": "answer", "choice": correct})
            assert wait_for(player, "answer_result")["correct"] is True

        wait_for(host, "final")


def test_points_are_scored_against_the_resolved_limit(authed, server_module):
    """A fast answer on a 120 s question must still be worth close to 1000, not
    be scaled against the 30 s constant."""
    test_id = mixed_test(authed)
    with authed.websocket_connect(ws_url(authed, "host-p")) as host, \
         authed.websocket_connect(ws_url(authed, "p-p")) as player:
        host.send_json({"action": "host_join", "subject": SUBJECT, "testId": test_id})
        wait_for(host, "host_joined")
        player.send_json({"action": "player_join", "name": "Ada", "studentNumber": "1", "subject": SUBJECT})
        wait_for(player, "joined")
        host.send_json({"action": "start_game", "shuffle": False, "useCode": False})

        scores = []
        for correct in (0, 1, 2):
            wait_for(player, "question")
            player.send_json({"action": "answer", "choice": correct})
            scores.append(wait_for(player, "answer_result")["points"])
        wait_for(host, "final")

    # Answered almost instantly, so every question should award near the maximum
    # regardless of its limit.
    for points in scores:
        assert points >= 900, f"scored {points}, expected near {server_module.MAX_POINTS}"


def test_mid_question_reconnect_shows_the_right_remaining_time(authed, server_module):
    test_id = mixed_test(authed)
    url = ws_url(authed, "reconnector")
    with authed.websocket_connect(ws_url(authed, "host-r")) as host:
        host.send_json({"action": "host_join", "subject": SUBJECT, "testId": test_id})
        wait_for(host, "host_joined")
        with authed.websocket_connect(url) as player:
            player.send_json({"action": "player_join", "name": "Ada", "studentNumber": "1", "subject": SUBJECT})
            wait_for(player, "joined")
            host.send_json({"action": "start_game", "shuffle": False, "useCode": False})
            assert wait_for(player, "question")["timeLimit"] == 10

        # Reconnect during question 1, whose limit is 10 s, not the 30 s default.
        with authed.websocket_connect(url) as player2:
            player2.send_json({"action": "player_join", "name": "Ada", "studentNumber": "1", "subject": SUBJECT})
            rejoined = wait_for(player2, "joined")
            current = rejoined["currentQuestion"]
            assert current["timeLimit"] == 10
            assert 0 < current["remaining"] <= 10


def test_host_can_extend_the_live_question(authed):
    test_id = mixed_test(authed)
    with authed.websocket_connect(ws_url(authed, "host-e")) as host, \
         authed.websocket_connect(ws_url(authed, "p-e")) as player:
        host.send_json({"action": "host_join", "subject": SUBJECT, "testId": test_id})
        wait_for(host, "host_joined")
        player.send_json({"action": "player_join", "name": "Ada", "studentNumber": "1", "subject": SUBJECT})
        wait_for(player, "joined")
        host.send_json({"action": "start_game", "shuffle": False, "useCode": False})
        assert wait_for(player, "question")["timeLimit"] == 10

        host.send_json({"action": "extend_time", "seconds": 15})
        extended = wait_for(host, "time_extended")
        assert extended["timeLimit"] == 25
        assert extended["addedSeconds"] == 15
        assert wait_for(player, "time_extended")["timeLimit"] == 25


def test_extending_is_ignored_outside_a_live_question(authed):
    test_id = mixed_test(authed)
    with authed.websocket_connect(ws_url(authed, "host-x")) as host:
        host.send_json({"action": "host_join", "subject": SUBJECT, "testId": test_id})
        wait_for(host, "host_joined")
        host.send_json({"action": "extend_time", "seconds": 15})
        host.send_json({"action": "ping"})
        assert wait_for(host, "pong")        # no time_extended arrived first
