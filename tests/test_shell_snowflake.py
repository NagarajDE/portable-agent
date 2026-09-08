"""Snowflake shell: /invoke + /feedback, input validation, feedback truthfulness.
The shell reads env + builds memory at import, so we set env via monkeypatch and reload the
module inside a fixture (no collection-time env leak; N5)."""
import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("WORKER_PROVIDER", "mock")
    monkeypatch.setenv("SQL_TOOL", "mock")
    monkeypatch.setenv("TRACER", "none")
    monkeypatch.setenv("MEMORY_STORE", "mock")
    import engine.memory as memory
    memory._instance = None                          # rebuild the store under mock
    from engine.platform_snowflake import agent
    importlib.reload(agent)                          # re-run module with env set
    return TestClient(agent.app)


def test_invoke_returns_answer_score_runid(client):
    r = client.post("/invoke", json={"question": "duplicate lots?"}).json()
    assert set(r) == {"answer", "score", "run_id"} and len(r["run_id"]) == 12


def test_feedback_recorded_with_mock_memory(client):
    r = client.post("/invoke", json={"question": "dupes?"}).json()
    fb = client.post("/feedback", json={"run_id": r["run_id"], "rating": "up"}).json()
    assert fb["status"] == "recorded"


def test_healthz_reports_effective_memory(client):
    h = client.get("/healthz").json()
    assert h["memory"] == "mock" and h["memory_effective"] == "mock"


def test_empty_question_rejected(client):
    assert client.post("/invoke", json={"question": "   "}).status_code == 422


def test_feedback_blank_rating_rejected(client):
    assert client.post("/feedback", json={"run_id": "r1", "rating": ""}).status_code == 422


def test_feedback_whitespace_rating_rejected(client):
    assert client.post("/feedback", json={"run_id": "r1", "rating": "   "}).status_code == 422


def test_feedback_oversized_note_rejected(client):
    r = client.post("/feedback", json={"run_id": "r1", "rating": "up", "note": "x" * 3000})
    assert r.status_code == 422
