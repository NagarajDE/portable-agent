"""Tests for the (deliberately simple) memory module: backend selection, the mock and
sqlite adapters, run_id validation, and the fail-safe remember_run boundary."""
import pytest

from engine import memory
from engine.memory import (
    MockMemory, NullMemory, SqliteMemory, get_memory, memory_enabled, remember_run,
)


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    """Each test starts with a fresh singleton and no MEMORY_STORE env."""
    monkeypatch.delenv("MEMORY_STORE", raising=False)
    memory._instance = None
    yield
    memory._instance = None


# --- backend selection -----------------------------------------------------
def test_default_is_disabled():
    assert memory_enabled() is False
    assert isinstance(get_memory(), NullMemory)


def test_mock_selected(monkeypatch):
    monkeypatch.setenv("MEMORY_STORE", "mock")
    assert memory_enabled() is True
    assert isinstance(get_memory(), MockMemory)


def test_unknown_backend_raises(monkeypatch):
    monkeypatch.setenv("MEMORY_STORE", "snowfalke")   # typo must not silently disable
    with pytest.raises(ValueError):
        memory_enabled()


def test_get_memory_is_memoized(monkeypatch):
    monkeypatch.setenv("MEMORY_STORE", "mock")
    assert get_memory() is get_memory()


# --- NullMemory ------------------------------------------------------------
def test_null_memory_is_noop():
    m = NullMemory()
    m.log_interaction("r1", "dq", "q", "a", 18, 3)     # must not raise
    m.record_feedback("r1", "up")


# --- MockMemory ------------------------------------------------------------
def test_mock_logs_interaction_and_feedback():
    m = MockMemory()
    m.log_interaction("r1", "dq_qals", "question?", "answer.", 18, 3)
    m.record_feedback("r1", "up", "good")
    assert len(m.interactions) == 1 and m.interactions[0]["run_id"] == "r1"
    assert m.interactions[0]["score"] == 18
    assert len(m.feedback) == 1 and m.feedback[0]["rating"] == "up"


@pytest.mark.parametrize("bad", ["", "   ", None, 123])
def test_run_id_validation(bad):
    m = MockMemory()
    with pytest.raises(ValueError):
        m.log_interaction(bad, "dq", "q", "a", 18, 3)


# --- SqliteMemory ----------------------------------------------------------
def test_sqlite_persists_and_reads_back(tmp_path):
    import sqlite3
    db = tmp_path / "mem.db"
    m = SqliteMemory(path=str(db))
    m.log_interaction("r1", "dq_qals", "question?", "answer.", 17, 2)
    m.record_feedback("r1", "down", "missed root cause")
    con = sqlite3.connect(str(db))
    try:
        rows = con.execute(
            "SELECT run_id, use_case, score, iterations FROM interactions").fetchall()
        fb = con.execute("SELECT run_id, rating FROM feedback").fetchall()
    finally:
        con.close()
    assert rows == [("r1", "dq_qals", 17, 2)]
    assert fb == [("r1", "down")]


def test_sqlite_rejects_bad_run_id(tmp_path):
    m = SqliteMemory(path=str(tmp_path / "mem.db"))
    with pytest.raises(ValueError):
        m.log_interaction("", "dq", "q", "a", 18, 3)


# --- remember_run (fail-safe boundary) -------------------------------------
def test_remember_run_captures_with_mock(monkeypatch):
    monkeypatch.setenv("MEMORY_STORE", "mock")
    state = {"run_id": "r9", "task": "q?", "best_answer": "a", "best_score": 18,
             "iterations": 3}
    remember_run(state, "dq_qals")
    assert get_memory().interactions[0]["run_id"] == "r9"


def test_remember_run_swallows_store_errors():
    class Boom:
        def log_interaction(self, *a, **k):
            raise RuntimeError("db down")
    memory._instance = Boom()
    remember_run({"run_id": "r1"}, "dq_qals")          # must NOT raise


def test_remember_run_swallows_bad_state():
    memory._instance = MockMemory()
    remember_run({}, "dq_qals")                        # empty run_id -> validation error, swallowed
    assert get_memory().interactions == []
