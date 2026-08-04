"""Lecturer signup / login / session cookie round trip."""

from conftest import signup


def test_signup_sets_session_cookie_and_session_endpoint_agrees(client):
    assert client.get("/api/lecturer/session").json()["authenticated"] is False

    lecturer = signup(client)
    assert lecturer["email"] == "lecturer@example.com"
    assert "lecturer_session" in client.cookies

    session = client.get("/api/lecturer/session").json()
    assert session["authenticated"] is True
    assert session["lecturer"]["id"] == lecturer["id"]


def test_login_round_trip_after_cookie_cleared(client):
    lecturer = signup(client)
    client.cookies.clear()
    assert client.get("/api/lecturer/session").json()["authenticated"] is False

    resp = client.post(
        "/api/lecturer/login",
        json={"email": "lecturer@example.com", "password": "test-password-1"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["lecturer"]["id"] == lecturer["id"]
    assert client.get("/api/lecturer/session").json()["authenticated"] is True


def test_login_rejects_wrong_password(client):
    signup(client)
    client.cookies.clear()
    resp = client.post(
        "/api/lecturer/login",
        json={"email": "lecturer@example.com", "password": "not-the-password"},
    )
    assert resp.status_code == 401


def test_duplicate_signup_is_rejected(client):
    signup(client)
    resp = client.post(
        "/api/lecturer/signup",
        json={"name": "Someone Else", "email": "lecturer@example.com", "password": "another-password"},
    )
    assert resp.status_code == 409


def test_logout_clears_the_session(client):
    signup(client)
    assert client.post("/api/lecturer/logout", json={}).status_code == 200
    assert client.get("/api/lecturer/session").json()["authenticated"] is False


def test_lecturer_account_survives_a_restart(client, restart, store_path):
    from fastapi.testclient import TestClient

    signup(client)
    module = restart()
    with TestClient(module.app) as fresh:
        resp = fresh.post(
            "/api/lecturer/login",
            json={"email": "lecturer@example.com", "password": "test-password-1"},
        )
        assert resp.status_code == 200, resp.text
