"""Phase 4: security and privacy."""

import importlib
import sys

import pytest
from conftest import SUBJECT, load_server, make_question, make_test_payload, signup, ws_url
from fastapi.testclient import TestClient
from test_game_flow import wait_for


@pytest.fixture()
def authed(client):
    signup(client)
    return client


# ── 1. Student personal data behind lecturer auth ────────────────────────────

def play_one_question_game(client):
    payload = make_test_payload(questions=[make_question(1, correct=0)])
    test_id = client.post(f"/api/tests/{SUBJECT}", json=payload).json()["test"]["id"]
    with client.websocket_connect(ws_url(client, "host-sec")) as host, \
         client.websocket_connect(ws_url(client, "p-sec")) as player:
        host.send_json({"action": "host_join", "subject": SUBJECT, "testId": test_id, "sessionName": "Sec"})
        wait_for(host, "host_joined")
        player.send_json({"action": "player_join", "name": "Ada Lovelace", "studentNumber": "221000001", "subject": SUBJECT})
        wait_for(player, "joined")
        host.send_json({"action": "start_game", "shuffle": False, "useCode": False})
        wait_for(player, "question")
        player.send_json({"action": "answer", "choice": 0})
        wait_for(player, "answer_result")
        wait_for(host, "final")


def test_stats_spreadsheet_requires_lecturer_auth(authed):
    """It contains names, student numbers and per-question results, and was
    downloadable by anyone who knew a subject code."""
    play_one_question_game(authed)

    assert authed.get(f"/api/stats/{SUBJECT}").status_code == 200

    authed.cookies.clear()
    anonymous = authed.get(f"/api/stats/{SUBJECT}")
    assert anonymous.status_code == 401


def test_end_of_game_auto_download_still_works(authed):
    """The host browser is signed in, so its fetch carries the session cookie.
    Adding auth must not break the automatic download at the end of a game."""
    play_one_question_game(authed)
    # Exactly what the host page does after the "final" message.
    resp = authed.get(f"/api/stats/{SUBJECT}")
    assert resp.status_code == 200
    assert "spreadsheetml" in resp.headers["content-type"]
    assert resp.headers["content-disposition"].startswith("attachment;")
    assert len(resp.content) > 1000


# ── 2. Server-signed student identity ────────────────────────────────────────

def test_visitor_identity_cannot_be_chosen_by_the_client(authed):
    """A bare visitorId query parameter is no longer trusted."""
    with authed.websocket_connect("/ws?visitorId=someone-elses-id") as ws:
        ws.send_json({"action": "player_join", "name": "Impostor", "studentNumber": "9", "subject": SUBJECT})
        joined = wait_for(ws, "joined")
        assert joined["playerId"] != "someone-elses-id"


def test_a_forged_visitor_token_is_rejected(authed, server_module):
    import base64

    forged = base64.urlsafe_b64encode(b"victim-id.99999999999.deadbeef").decode()
    with authed.websocket_connect(f"/ws?vt={forged}") as ws:
        ws.send_json({"action": "player_join", "name": "Impostor", "studentNumber": "9", "subject": SUBJECT})
        assert wait_for(ws, "joined")["playerId"] != "victim-id"


def test_a_valid_visitor_token_round_trips(authed, server_module):
    resp = authed.post("/api/visitor-token", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert server_module.parse_visitor_token(body["token"]) == body["visitorId"]

    # Presenting it again keeps the same identity, so a reconnecting student
    # rejoins as themselves.
    again = authed.post("/api/visitor-token", json={"token": body["token"]}).json()
    assert again["visitorId"] == body["visitorId"]


def test_a_reconnecting_student_keeps_their_score(authed):
    payload = make_test_payload(questions=[make_question(1, correct=0), make_question(2, correct=0)])
    test_id = authed.post(f"/api/tests/{SUBJECT}", json=payload).json()["test"]["id"]
    url = ws_url(authed, "steady-student")

    with authed.websocket_connect(ws_url(authed, "host-rc")) as host:
        host.send_json({"action": "host_join", "subject": SUBJECT, "testId": test_id})
        wait_for(host, "host_joined")
        with authed.websocket_connect(url) as player:
            player.send_json({"action": "player_join", "name": "Ada", "studentNumber": "1", "subject": SUBJECT})
            wait_for(player, "joined")
            host.send_json({"action": "start_game", "shuffle": False, "useCode": False})
            wait_for(player, "question")
            player.send_json({"action": "answer", "choice": 0})
            score = wait_for(player, "answer_result")["totalScore"]
            assert score > 0

        # Same signed token → same identity → same score.
        with authed.websocket_connect(url) as player2:
            player2.send_json({"action": "player_join", "name": "Ada", "studentNumber": "1", "subject": SUBJECT})
            rejoined = wait_for(player2, "joined")
            assert rejoined["playerId"] == "steady-student"


# ── 3. Signup gating ─────────────────────────────────────────────────────────

def test_signup_stays_open_when_nothing_is_configured(client):
    """Neither variable set must behave exactly as before, so deploying this
    change cannot lock anyone out of a running deployment."""
    resp = client.post(
        "/api/lecturer/signup",
        json={"name": "Anyone", "email": "anyone@gmail.com", "password": "a-password-1"},
    )
    assert resp.status_code == 200


def test_email_domain_allowlist(monkeypatch, store_path):
    module = load_server(monkeypatch, store_path, ALLOWED_EMAIL_DOMAINS="tut.ac.za, example.edu")
    with TestClient(module.app) as client:
        rejected = client.post(
            "/api/lecturer/signup",
            json={"name": "Outsider", "email": "someone@gmail.com", "password": "a-password-1"},
        )
        assert rejected.status_code == 403
        assert "tut.ac.za" in rejected.json()["detail"]

        for email in ("lecturer@tut.ac.za", "prof@eng.tut.ac.za", "staff@example.edu"):
            ok = client.post(
                "/api/lecturer/signup",
                json={"name": "Colleague", "email": email, "password": "a-password-1"},
            )
            assert ok.status_code == 200, f"{email}: {ok.text}"
            client.cookies.clear()


def test_invite_code_is_accepted_as_an_alternative(monkeypatch, store_path):
    module = load_server(
        monkeypatch, store_path,
        ALLOWED_EMAIL_DOMAINS="tut.ac.za",
        SIGNUP_INVITE_CODE="let-me-in",
    )
    with TestClient(module.app) as client:
        blocked = client.post(
            "/api/lecturer/signup",
            json={"name": "Guest", "email": "guest@gmail.com", "password": "a-password-1"},
        )
        assert blocked.status_code == 403

        allowed = client.post(
            "/api/lecturer/signup",
            json={"name": "Guest", "email": "guest@gmail.com", "password": "a-password-1", "inviteCode": "let-me-in"},
        )
        assert allowed.status_code == 200, allowed.text


def test_invite_code_alone_gates_signup(monkeypatch, store_path):
    module = load_server(monkeypatch, store_path, SIGNUP_INVITE_CODE="secret-code")
    with TestClient(module.app) as client:
        assert client.post(
            "/api/lecturer/signup",
            json={"name": "Guest", "email": "guest@gmail.com", "password": "a-password-1"},
        ).status_code == 403
        assert client.post(
            "/api/lecturer/signup",
            json={"name": "Guest", "email": "guest@gmail.com", "password": "a-password-1", "inviteCode": "secret-code"},
        ).status_code == 200


# ── 4. Session secret ────────────────────────────────────────────────────────

def test_refuses_to_start_in_production_without_a_session_secret(monkeypatch, store_path):
    monkeypatch.setenv("LOCAL_STORE_PATH", str(store_path))
    monkeypatch.setenv("SUPABASE_URL", "https://project.supabase.co")
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    monkeypatch.delenv("APP_SESSION_SECRET", raising=False)
    sys.modules.pop("server", None)
    with pytest.raises(RuntimeError, match="APP_SESSION_SECRET"):
        importlib.import_module("server")
    sys.modules.pop("server", None)


def test_seeding_the_secret_with_the_service_role_key_keeps_sessions_valid(monkeypatch, store_path):
    """The migration path the owner is told to use: set APP_SESSION_SECRET to
    the current SUPABASE_SERVICE_ROLE_KEY so nobody is signed out."""
    service_key = "the-current-service-role-key"

    # Before: no APP_SESSION_SECRET, so sessions are signed with the service key.
    before = load_server(monkeypatch, store_path, SUPABASE_SERVICE_ROLE_KEY=service_key)
    monkeypatch.delenv("APP_SESSION_SECRET", raising=False)
    token = before.create_session_token("lecturer-123")

    # After: APP_SESSION_SECRET seeded with that same value, as documented.
    after = load_server(monkeypatch, store_path, APP_SESSION_SECRET=service_key)
    assert after.parse_session_token(token) == "lecturer-123", \
        "seeding APP_SESSION_SECRET with the service role key must not sign anyone out"

    # And a genuinely different secret does invalidate it, as it should.
    rotated = load_server(monkeypatch, store_path, APP_SESSION_SECRET="a-fresh-random-secret")
    assert rotated.parse_session_token(token) is None


def test_local_development_still_works_without_a_secret(monkeypatch, store_path):
    module = load_server(monkeypatch, store_path)
    monkeypatch.delenv("APP_SESSION_SECRET", raising=False)
    assert module.parse_session_token(module.create_session_token("abc")) == "abc"


# ── 5. PostgREST filter injection ────────────────────────────────────────────

@pytest.mark.parametrize("bad_id", [
    "abc,def",
    "00000000-0000-0000-0000-000000000000,created_by.neq.null",
    "*",
    "(1)",
    "../../etc",
])
def test_malformed_test_ids_are_rejected_at_the_boundary(authed, bad_id):
    assert authed.get(f"/api/tests/{SUBJECT}/{bad_id}").status_code in (400, 404)
    assert authed.delete(f"/api/tests/{SUBJECT}/{bad_id}").status_code in (400, 404)


def test_filter_values_with_postgrest_metacharacters_are_refused(server_module):
    store = server_module.SupabaseStore("https://example.supabase.co", "key")
    with pytest.raises(ValueError):
        store._check_filter_params({"id": "eq.abc,created_by.neq.null"})
    with pytest.raises(ValueError):
        store._check_filter_params({"subject_code": "eq.MEC105B)"})
    # Ordinary values pass, including emails with dots and plus signs.
    store._check_filter_params({"email": "eq.lecturer+tag@tut.ac.za", "select": "id,name,email"})


def test_safe_id_helpers(server_module):
    good = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"
    assert server_module.safe_test_id(good) == good
    assert server_module.safe_test_id(f"local:{good}") == f"local:{good}"
    assert server_module.safe_subject_code("mec105b") == "MEC105B"
    for bad in ("", "not-a-uuid", f"{good},x", "local:nope"):
        with pytest.raises(ValueError):
            server_module.safe_test_id(bad)


# ── 6. CORS ──────────────────────────────────────────────────────────────────

def test_cors_does_not_default_to_wildcard(server_module):
    assert server_module.allow_origins != ["*"]


def test_cors_wildcard_is_opt_in(monkeypatch, store_path):
    module = load_server(monkeypatch, store_path, ALLOWED_ORIGINS="*")
    assert module.allow_origins == ["*"]


# ── 7. Rate limiting ─────────────────────────────────────────────────────────

def test_draft_autosave_is_not_rate_limited_in_practice(authed):
    """Autosave fires 1.5 s after typing stops. A tight limit here would break
    the feature Phase 2 exists to fix."""
    payload = {"title": "Typing", "questions": [], "editingTestId": None}
    for i in range(40):
        resp = authed.post(f"/api/drafts/{SUBJECT}", json={**payload, "title": f"Typing {i}"})
        assert resp.status_code == 200, f"draft save {i} was rejected: {resp.text}"


def test_test_creation_is_rate_limited(monkeypatch, store_path):
    module = load_server(monkeypatch, store_path)
    with TestClient(module.app) as client:
        signup(client)
        statuses = [
            client.post(f"/api/tests/{SUBJECT}", json=make_test_payload(title=f"T{i}")).status_code
            for i in range(40)
        ]
        assert 429 in statuses, "test creation should be rate limited"
        assert statuses[0] == 200, "the limit must not bite immediately"


# ── 8. Information leakage ───────────────────────────────────────────────────

def test_health_hides_supabase_error_detail_from_anonymous_callers(authed, server_module):
    server_module.repo.supabase_error = "PGRST205: table 'quiz_tests' missing in project abcxyz"
    server_module.repo.supabase_configured = True

    signed_in = authed.get("/api/health").json()
    assert "abcxyz" in signed_in["storage"]["supabaseError"]

    authed.cookies.clear()
    public = authed.get("/api/health").json()
    assert "supabaseError" not in public["storage"]
    assert public["storage"]["healthy"] is False
    assert public["ok"] is True


def test_storage_status_hides_error_detail_too(authed, server_module):
    server_module.repo.supabase_error = "secret detail"
    assert "secret detail" == authed.get("/api/storage-status").json()["supabaseError"]
    authed.cookies.clear()
    assert "supabaseError" not in authed.get("/api/storage-status").json()
