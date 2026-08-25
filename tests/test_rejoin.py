"""A student who was in the game must always be able to get back in.

Reported in production: a student was in the quiz, got disconnected as the game
started, and every attempt to rejoin was refused with "The game is already in
progress. You cannot join as a new player at this stage."
"""

import pytest
from conftest import SUBJECT, make_question, make_test_payload, signup, ws_url
from test_game_flow import wait_for


@pytest.fixture()
def authed(client):
    signup(client)
    return client


@pytest.fixture()
def test_id(authed):
    payload = make_test_payload(questions=[make_question(i, correct=0) for i in range(4)])
    return authed.post(f"/api/tests/{SUBJECT}", json=payload).json()["test"]["id"]


def start_game_with_student(client, test_id, student_visitor="ada", number="221012345"):
    """Host a game with one student in it, and leave it running mid-question."""
    host = client.websocket_connect(ws_url(client, "host")).__enter__()
    host.send_json({"action": "host_join", "subject": SUBJECT, "testId": test_id, "newSession": True})
    joined = wait_for(host, "host_joined")
    token = joined.get("sessionToken") or ""

    player = client.websocket_connect(ws_url(client, student_visitor)).__enter__()
    player.send_json({
        "action": "player_join", "name": "Ada", "studentNumber": number,
        "subject": SUBJECT, "token": token,
    })
    wait_for(player, "joined")
    host.send_json({"action": "start_game", "shuffle": False, "useCode": False})
    wait_for(player, "question")
    return host, player, token


def rejoin(client, visitor, number, token="", name="Ada"):
    ws = client.websocket_connect(ws_url(client, visitor)).__enter__()
    ws.send_json({
        "action": "player_join", "name": name, "studentNumber": number,
        "subject": SUBJECT, "token": token,
    })
    return ws, ws.receive_json()


def test_a_kicked_student_can_rejoin_mid_game(authed, test_id, server_module):
    host, player, token = start_game_with_student(authed, test_id)
    try:
        player_id = next(iter(server_module.rooms[SUBJECT].players))
        host.send_json({"action": "kick_player", "playerId": player_id})
        wait_for(player, "kicked")

        ws, reply = rejoin(authed, "ada", "221012345", token)
        try:
            assert reply["type"] == "joined", f"kicked student refused: {reply}"
        finally:
            ws.__exit__(None, None, None)
    finally:
        for sock in (player, host):
            try:
                sock.__exit__(None, None, None)
            except Exception:
                pass


def test_a_student_whose_record_was_removed_can_still_rejoin(authed, test_id, server_module):
    """The worst case: nothing of them is left in room.players."""
    host, player, token = start_game_with_student(authed, test_id)
    try:
        room = server_module.rooms[SUBJECT]
        room.players.clear()                      # they are gone entirely
        assert room.phase != "lobby"

        ws, reply = rejoin(authed, "ada", "221012345", token)
        try:
            assert reply["type"] == "joined", f"student refused: {reply}"
        finally:
            ws.__exit__(None, None, None)
    finally:
        for sock in (player, host):
            try:
                sock.__exit__(None, None, None)
            except Exception:
                pass


def test_a_student_rejoining_from_a_new_browser_identity_is_recognised(authed, test_id, server_module):
    """Their signed visitor token was lost, so the server sees a new identity."""
    host, player, token = start_game_with_student(authed, test_id)
    try:
        server_module.rooms[SUBJECT].players.clear()
        # Different visitor id entirely — a fresh browser or a lost token.
        ws, reply = rejoin(authed, "ada-on-a-different-phone", "221012345", token)
        try:
            assert reply["type"] == "joined", f"student refused: {reply}"
        finally:
            ws.__exit__(None, None, None)
    finally:
        for sock in (player, host):
            try:
                sock.__exit__(None, None, None)
            except Exception:
                pass


@pytest.mark.parametrize("retyped", ["221012345", "221 012 345", "221-012-345", " 221012345 "])
def test_student_number_formatting_does_not_block_a_rejoin(authed, test_id, server_module, retyped):
    host, player, token = start_game_with_student(authed, test_id, number="221012345")
    try:
        server_module.rooms[SUBJECT].players.clear()
        ws, reply = rejoin(authed, "ada-again", retyped, token)
        try:
            assert reply["type"] == "joined", f"{retyped!r} was refused: {reply}"
        finally:
            ws.__exit__(None, None, None)
    finally:
        for sock in (player, host):
            try:
                sock.__exit__(None, None, None)
            except Exception:
                pass


def test_a_rejoining_student_keeps_their_score(authed, test_id, server_module):
    host, player, token = start_game_with_student(authed, test_id)
    try:
        player.send_json({"action": "answer", "choice": 0})
        earned = wait_for(player, "answer_result")["totalScore"]
        assert earned > 0

        room = server_module.rooms[SUBJECT]
        room.players.clear()

        ws, reply = rejoin(authed, "ada-back", "221012345", token)
        try:
            assert reply["type"] == "joined"
            restored = next(iter(room.players.values()))
            assert restored["score"] == earned, "the student lost their score on rejoin"
        finally:
            ws.__exit__(None, None, None)
    finally:
        for sock in (player, host):
            try:
                sock.__exit__(None, None, None)
            except Exception:
                pass


def test_a_genuinely_new_student_is_still_refused_mid_game(authed, test_id):
    """The protection this bug came from must stay intact."""
    host, player, token = start_game_with_student(authed, test_id)
    try:
        ws, reply = rejoin(authed, "stranger", "999999999", token, name="Newcomer")
        try:
            assert reply["type"] == "error"
            assert "already in progress" in reply["message"]
        finally:
            ws.__exit__(None, None, None)
    finally:
        for sock in (player, host):
            try:
                sock.__exit__(None, None, None)
            except Exception:
                pass


def test_the_roster_does_not_leak_across_sessions(authed, test_id, server_module):
    """A student from a finished game is not automatically in the next one."""
    host, player, token = start_game_with_student(authed, test_id)
    try:
        host.send_json({"action": "end_game"})
        wait_for(host, "final")
    finally:
        for sock in (player, host):
            try:
                sock.__exit__(None, None, None)
            except Exception:
                pass

    room = server_module.rooms[SUBJECT]
    assert room.session_roster == {}, "the roster must not survive the end of a game"
