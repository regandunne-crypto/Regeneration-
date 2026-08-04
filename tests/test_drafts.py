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


def test_drafts_are_per_lecturer(authed, client):
    authed.post(f"/api/drafts/{SUBJECT}", json=draft_payload(title="Mine"))
    authed.cookies.clear()
    signup(authed, email="other@example.com", name="Other Lecturer")
    assert authed.get(f"/api/drafts/{SUBJECT}").json()["draft"] is None
