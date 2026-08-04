"""Draft save → read back → survive a restart.

This is the regression suite for the reported bug: "I click Save Draft, exit,
come back in, and there is no saved draft."
"""

import pytest
from fastapi.testclient import TestClient

from conftest import SUBJECT, signup


def draft_payload(title="Work in progress", editing_test_id=None, question_count=2):
    return {
        "title": title,
        "chapter": "Chapter 4",
        "description": "Half finished",
        "questions": [
            {
                "q": f"Draft question {i}",
                "options": [f"A{i}", f"B{i}", f"C{i}", f"D{i}"],
                "correct": i % 4,
                "explanation": f"Because {i}",
            }
            for i in range(question_count)
        ],
        "editingTestId": editing_test_id,
    }


@pytest.fixture()
def authed(client):
    signup(client)
    return client


def test_draft_save_and_read_back(authed):
    assert authed.get(f"/api/drafts/{SUBJECT}").json()["draft"] is None

    saved = authed.post(f"/api/drafts/{SUBJECT}", json=draft_payload())
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["ok"] is True

    read_back = authed.get(f"/api/drafts/{SUBJECT}").json()["draft"]
    assert read_back is not None
    assert read_back["title"] == "Work in progress"
    assert len(read_back["questions"]) == 2
    assert read_back["questions"][0]["q"] == "Draft question 0"
    assert read_back["editing_test_id"] is None


def test_create_mode_draft_has_null_editing_test_id(authed):
    authed.post(f"/api/drafts/{SUBJECT}", json=draft_payload(editing_test_id=None))
    draft = authed.get(f"/api/drafts/{SUBJECT}").json()["draft"]
    # A draft for a brand new test carries no editing_test_id. The old client
    # only ever restored drafts whose id matched the test being edited, which is
    # why new-test drafts could never be recovered.
    assert draft["editing_test_id"] is None
    assert draft["title"] == "Work in progress"


def test_draft_survives_a_simulated_restart(authed, restart):
    authed.post(f"/api/drafts/{SUBJECT}", json=draft_payload(title="Survives restart"))

    module = restart()
    with TestClient(module.app) as fresh:
        fresh.post("/api/lecturer/login", json={"email": "lecturer@example.com", "password": "test-password-1"})
        draft = fresh.get(f"/api/drafts/{SUBJECT}").json()["draft"]
        assert draft is not None, "draft was lost across a restart"
        assert draft["title"] == "Survives restart"
        assert len(draft["questions"]) == 2


def test_draft_is_overwritten_not_duplicated(authed):
    authed.post(f"/api/drafts/{SUBJECT}", json=draft_payload(title="First"))
    authed.post(f"/api/drafts/{SUBJECT}", json=draft_payload(title="Second", question_count=3))
    draft = authed.get(f"/api/drafts/{SUBJECT}").json()["draft"]
    assert draft["title"] == "Second"
    assert len(draft["questions"]) == 3


def test_saving_a_test_clears_the_draft(authed):
    from conftest import make_test_payload

    authed.post(f"/api/drafts/{SUBJECT}", json=draft_payload())
    assert authed.get(f"/api/drafts/{SUBJECT}").json()["draft"] is not None

    assert authed.post(f"/api/tests/{SUBJECT}", json=make_test_payload()).status_code == 200
    assert authed.get(f"/api/drafts/{SUBJECT}").json()["draft"] is None


def test_explicit_discard_clears_the_draft(authed):
    authed.post(f"/api/drafts/{SUBJECT}", json=draft_payload())
    assert authed.delete(f"/api/drafts/{SUBJECT}").status_code == 200
    assert authed.get(f"/api/drafts/{SUBJECT}").json()["draft"] is None


def test_drafts_require_auth(client):
    assert client.get(f"/api/drafts/{SUBJECT}").status_code == 401
    assert client.post(f"/api/drafts/{SUBJECT}", json=draft_payload()).status_code == 401


def test_save_reports_which_backend_stored_the_draft(authed):
    body = authed.post(f"/api/drafts/{SUBJECT}", json=draft_payload()).json()
    assert body["storedIn"] == "local-file"
    assert body["degraded"] is False          # no Supabase configured, so not a downgrade
    assert "lost if the server redeploys" in body["storedInLabel"]

    read = authed.get(f"/api/drafts/{SUBJECT}").json()
    assert read["storedIn"] == "local-file"


def test_a_failed_draft_write_returns_an_error_not_a_soft_ok(authed, server_module):
    """The old endpoint returned HTTP 200 {"ok": false} and the editor showed
    "Draft saved <time>" for a draft that was never stored."""

    async def explode(*args, **kwargs):
        raise RuntimeError("storage backend is on fire")

    server_module.repo.save_draft = explode

    resp = authed.post(f"/api/drafts/{SUBJECT}", json=draft_payload())
    assert resp.status_code == 500
    assert "on fire" in resp.json()["detail"]
    # And crucially: not a success shape the client could mistake for one.
    assert resp.json().get("ok") is not True


def test_supabase_draft_failure_falls_back_to_local_and_says_so(authed, server_module):
    """Simulate Supabase being unreachable for drafts only.

    The draft must still be stored, the response must name the backend that
    actually took it, and the Supabase connection used for tests must stay up.
    """

    class BrokenDrafts:
        """Supabase is up for everything except the drafts table."""

        drafts_base = "https://example.invalid/rest/v1/quiz_test_drafts"

        async def get_lecturer_by_id(self, *args, **kwargs):
            return None      # falls through to the local lecturer cache

        async def get_lecturer_by_email(self, *args, **kwargs):
            return None

        async def get_draft(self, *args, **kwargs):
            raise RuntimeError("PGRST205 Could not find the table 'quiz_test_drafts'")

        async def save_draft(self, *args, **kwargs):
            raise RuntimeError("PGRST205 Could not find the table 'quiz_test_drafts'")

        async def clear_draft(self, *args, **kwargs):
            raise RuntimeError("PGRST205 Could not find the table 'quiz_test_drafts'")

    repo = server_module.repo
    repo.remote = BrokenDrafts()
    repo.supabase_configured = True

    body = authed.post(f"/api/drafts/{SUBJECT}", json=draft_payload(title="Fallback")).json()
    assert body["ok"] is True
    assert body["storedIn"] == "local-file"
    assert body["degraded"] is True                  # Supabase was expected to take it
    assert "quiz_test_drafts" in (body["error"] or "")

    # The draft is genuinely retrievable, not just reported as saved.
    assert authed.get(f"/api/drafts/{SUBJECT}").json()["draft"]["title"] == "Fallback"
    # A draft-table failure must not disable Supabase for everything else.
    assert repo.remote is not None


def test_diagnostics_reports_table_reachability(authed, server_module):
    resp = authed.get("/api/diagnostics")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body["tables"]) == {"quiz_lecturers", "quiz_tests", "quiz_test_drafts", "quiz_subjects"}
    for table, info in body["tables"].items():
        assert info["reachable"] is False, table
        assert "not configured" in info["error"]
    assert body["localStore"]["enabled"] is True
    assert body["localStore"]["exists"] is True


def test_diagnostics_requires_lecturer_auth(client):
    assert client.get("/api/diagnostics").status_code == 401


def test_drafts_are_per_lecturer(authed, client):
    authed.post(f"/api/drafts/{SUBJECT}", json=draft_payload(title="Mine"))
    authed.cookies.clear()
    signup(authed, email="other@example.com", name="Other Lecturer")
    assert authed.get(f"/api/drafts/{SUBJECT}").json()["draft"] is None
