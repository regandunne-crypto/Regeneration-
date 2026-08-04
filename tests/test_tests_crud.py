"""Create / list / get / update / delete for saved tests."""

import pytest
from conftest import SUBJECT, make_question, make_test_payload, signup


@pytest.fixture()
def authed(client):
    signup(client)
    return client


def test_full_crud_cycle(authed):
    created = authed.post(f"/api/tests/{SUBJECT}", json=make_test_payload())
    assert created.status_code == 200, created.text
    test_id = created.json()["test"]["id"]
    assert created.json()["test"]["questionCount"] == 2

    listed = authed.get(f"/api/tests/{SUBJECT}").json()
    assert [t["id"] for t in listed] == [test_id]
    assert listed[0]["canEdit"] is True

    detail = authed.get(f"/api/tests/{SUBJECT}/{test_id}").json()
    assert detail["title"] == "Chapter 1 Quiz"
    assert len(detail["questions"]) == 2
    assert detail["questions"][0]["options"] == ["Newton", "Joule", "Watt", "Pascal"]

    updated = authed.put(
        f"/api/tests/{SUBJECT}/{test_id}",
        json=make_test_payload(title="Renamed Quiz", questions=[make_question(9)]),
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["test"]["title"] == "Renamed Quiz"
    assert authed.get(f"/api/tests/{SUBJECT}/{test_id}").json()["questionCount"] == 1

    assert authed.delete(f"/api/tests/{SUBJECT}/{test_id}").status_code == 200
    assert authed.get(f"/api/tests/{SUBJECT}").json() == []
    assert authed.get(f"/api/tests/{SUBJECT}/{test_id}").status_code == 404


def test_tests_require_lecturer_auth(client):
    assert client.get(f"/api/tests/{SUBJECT}").status_code == 401
    assert client.post(f"/api/tests/{SUBJECT}", json=make_test_payload()).status_code == 401


def test_unknown_subject_is_404(authed):
    assert authed.get("/api/tests/NOSUCH").status_code == 404


def test_validation_rejects_bad_questions(authed):
    bad = make_test_payload(questions=[make_question(1, options=["only", "three", "options"])])
    assert authed.post(f"/api/tests/{SUBJECT}", json=bad).status_code == 422

    blank = make_test_payload(title="   ")
    assert authed.post(f"/api/tests/{SUBJECT}", json=blank).status_code == 422

    empty = make_test_payload(questions=[])
    assert authed.post(f"/api/tests/{SUBJECT}", json=empty).status_code == 422


def test_saved_test_survives_a_restart(authed, restart):
    from fastapi.testclient import TestClient

    test_id = authed.post(f"/api/tests/{SUBJECT}", json=make_test_payload()).json()["test"]["id"]

    module = restart()
    with TestClient(module.app) as fresh:
        fresh.post("/api/lecturer/login", json={"email": "lecturer@example.com", "password": "test-password-1"})
        detail = fresh.get(f"/api/tests/{SUBJECT}/{test_id}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["title"] == "Chapter 1 Quiz"
