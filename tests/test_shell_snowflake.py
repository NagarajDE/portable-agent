"""Snowflake shell: /invoke + /feedback, input validation, feedback truthfulness.
Env is set BEFORE importing the shell because it reads config + builds memory at import."""
import os

os.environ["WORKER_PROVIDER"] = "mock"
os.environ["SQL_TOOL"] = "mock"
os.environ["TRACER"] = "none"
os.environ["MEMORY_STORE"] = "mock"

from fastapi.testclient import TestClient          # noqa: E402
from engine.platform_snowflake.agent import app     # noqa: E402

client = TestClient(app)


def test_invoke_returns_answer_score_runid():
    r = client.post("/invoke", json={"question": "duplicate lots?"}).json()
    assert set(r) == {"answer", "score", "run_id"} and len(r["run_id"]) == 12


def test_feedback_recorded_with_mock_memory():
    r = client.post("/invoke", json={"question": "dupes?"}).json()
    fb = client.post("/feedback", json={"run_id": r["run_id"], "rating": "up"}).json()
    assert fb["status"] == "recorded"


def test_healthz_reports_effective_memory():
    h = client.get("/healthz").json()
    assert h["memory"] == "mock" and h["memory_effective"] == "mock"


def test_empty_question_rejected():
    assert client.post("/invoke", json={"question": "   "}).status_code == 422


def test_feedback_blank_rating_rejected():
    assert client.post("/feedback", json={"run_id": "r1", "rating": ""}).status_code == 422


def test_feedback_oversized_note_rejected():
    r = client.post("/feedback", json={"run_id": "r1", "rating": "up", "note": "x" * 3000})
    assert r.status_code == 422
