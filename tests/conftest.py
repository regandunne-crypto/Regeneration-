"""Shared fixtures.

`server.py` builds its repository and reads its configuration at import time, so
every test gets a freshly reloaded module pointed at its own `tmp_path` store.
That keeps tests hermetic and lets us simulate a server restart simply by
reloading the module against the same store file.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# Environment variables that must not leak in from the developer's shell.
_CLEARED_ENV = [
    "SUPABASE_URL",
    "SUPABASE_SERVICE_ROLE_KEY",
    "REQUIRE_SUPABASE",
    "ALLOWED_ORIGINS",
    "ALLOWED_EMAIL_DOMAINS",
    "SIGNUP_INVITE_CODE",
    "PERSIST_RESULTS",
    "RESULTS_RETENTION",
]


def _prepare_env(monkeypatch, store_path: Path, **overrides: str) -> None:
    for name in _CLEARED_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LOCAL_STORE_PATH", str(store_path))
    monkeypatch.setenv("APP_SESSION_SECRET", "test-session-secret")
    # Collapse the classroom pacing so a full game runs in milliseconds.
    monkeypatch.setenv("GET_READY_SECONDS", "0.05")
    monkeypatch.setenv("REVEAL_SECONDS", "0.1")
    monkeypatch.setenv("GAME_CODE_COUNTDOWN_SECONDS", "0.1")
    for key, value in overrides.items():
        monkeypatch.setenv(key, value)


def load_server(monkeypatch, store_path: Path, **overrides: str):
    """(Re)import `server` fresh against the given store file."""
    _prepare_env(monkeypatch, store_path, **overrides)
    sys.modules.pop("server", None)
    return importlib.import_module("server")


@pytest.fixture()
def store_path(tmp_path: Path) -> Path:
    return tmp_path / "local_store.json"


@pytest.fixture()
def server_module(monkeypatch, store_path: Path):
    module = load_server(monkeypatch, store_path)
    yield module
    sys.modules.pop("server", None)


@pytest.fixture()
def client(server_module):
    with TestClient(server_module.app) as test_client:
        yield test_client


@pytest.fixture()
def restart(monkeypatch, store_path: Path):
    """Simulate a process restart: reload the module, keep the store file."""

    def _restart(**overrides: str):
        module = load_server(monkeypatch, store_path, **overrides)
        return module

    return _restart


# ── Helpers ───────────────────────────────────────────────────────────────────

SUBJECT = "MEC105B"


def signup(client, email="lecturer@example.com", password="test-password-1", name="Dr Test"):
    resp = client.post("/api/lecturer/signup", json={"name": name, "email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["lecturer"]


def ws_url(client, visitor: str) -> str:
    """WebSocket URL carrying a server-signed identity for `visitor`.

    Student identity is no longer taken from a client-supplied query parameter,
    so tests must ask the server for a token like a real browser does.
    """
    resp = client.post("/api/visitor-token", json={})
    assert resp.status_code == 200, resp.text
    import server

    _, token = server.create_visitor_token(visitor)
    return f"/ws?vt={token}"


def make_question(index: int = 1, **overrides):
    question = {
        "q": f"Question {index}: what is the SI unit of force?",
        "options": ["Newton", "Joule", "Watt", "Pascal"],
        "correct": 0,
        "explanation": "Force is measured in newtons.",
    }
    question.update(overrides)
    return question


def make_test_payload(title="Chapter 1 Quiz", questions=None, **overrides):
    payload = {
        "title": title,
        "chapter": "Chapter 1",
        "description": "A short quiz.",
        "questions": questions if questions is not None else [make_question(1), make_question(2)],
    }
    payload.update(overrides)
    return payload
