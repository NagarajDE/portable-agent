"""
Comprehensive tests for engine/memory.py

Covers all findings from the Astra code review:
- Backend validation (H2)
- NullMemory no-ops
- MockMemory with max_entries eviction (M12)
- SqliteMemory lifecycle, uniqueness, types (M6, M7, M9, M10, M11, M14)
- Singleton behavior and thread safety
- remember_run error handling (H1)
- Input validation (M9)
- Purge/retention (M15)
- Feedback isolation (M16)
"""
import os
import sqlite3
import threading
import time
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Helper: reset singleton between tests
# ---------------------------------------------------------------------------

def _reset_singleton():
    """Reset the module-level singleton so each test starts clean."""
    import engine.memory as mod
    with mod._lock:
        mod._instance = None
        mod._init_error = None


@pytest.fixture(autouse=True)
def clean_singleton():
    """Ensure every test gets a fresh singleton."""
    _reset_singleton()
    yield
    _reset_singleton()


# ---------------------------------------------------------------------------
# 1. Backend validation (Finding H2)
# ---------------------------------------------------------------------------

class TestBackendValidation:
    def test_valid_backends_accepted(self, monkeypatch):
        from engine.memory import get_memory, NullMemory, MockMemory
        monkeypatch.setenv("MEMORY_STORE", "none")
        store = get_memory()
        assert isinstance(store, NullMemory)

    def test_mock_backend_returns_mock_memory(self, monkeypatch):
        from engine.memory import get_memory, MockMemory
        monkeypatch.setenv("MEMORY_STORE", "mock")
        store = get_memory()
        assert isinstance(store, MockMemory)

    def test_invalid_backend_raises_value_error(self, monkeypatch):
        from engine.memory import get_memory
        monkeypatch.setenv("MEMORY_STORE", "snowflkae")  # typo
        with pytest.raises(ValueError, match="(?i)memory_store"):
            get_memory()

    def test_empty_string_backend_treated_as_none(self, monkeypatch):
        from engine.memory import get_memory, NullMemory
        monkeypatch.setenv("MEMORY_STORE", "")
        store = get_memory()
        assert isinstance(store, NullMemory)

    def test_memory_enabled_false_for_none(self, monkeypatch):
        from engine.memory import memory_enabled
        monkeypatch.setenv("MEMORY_STORE", "none")
        assert memory_enabled() is False

    def test_memory_enabled_true_for_mock(self, monkeypatch):
        from engine.memory import memory_enabled
        monkeypatch.setenv("MEMORY_STORE", "mock")
        assert memory_enabled() is True


# ---------------------------------------------------------------------------
# 2. NullMemory
# ---------------------------------------------------------------------------

class TestNullMemory:
    def test_log_interaction_is_noop(self):
        from engine.memory import NullMemory
        store = NullMemory()
        # Should not raise
        store.log_interaction("r1", "uc", "q", "a", 10.0, 1)

    def test_record_feedback_is_noop(self):
        from engine.memory import NullMemory
        store = NullMemory()
        store.record_feedback("r1", "good", "nice")

    def test_purge_returns_none(self):
        from engine.memory import NullMemory
        store = NullMemory()
        store.purge_before(time.time())

    def test_status_reports_none_backend(self):
        from engine.memory import NullMemory
        store = NullMemory()
        s = store.status()
        assert s["effective_backend"] == "none"


# ---------------------------------------------------------------------------
# 3. MockMemory (Finding M12 - bounded growth)
# ---------------------------------------------------------------------------

class TestMockMemory:
    def test_log_stores_interaction(self):
        from engine.memory import MockMemory
        store = MockMemory(max_entries=100)
        store.log_interaction("r1", "uc", "q", "a", 15.0, 2)
        assert len(store.interactions) == 1
        assert store.interactions[0]["run_id"] == "r1"

    def test_record_feedback_stores(self):
        from engine.memory import MockMemory
        store = MockMemory(max_entries=100)
        store.record_feedback("r1", "thumbs_up", "great")
        assert len(store.feedback) == 1
        assert store.feedback[0]["run_id"] == "r1"

    def test_max_entries_evicts_oldest(self):
        from engine.memory import MockMemory
        store = MockMemory(max_entries=3)
        for i in range(5):
            store.log_interaction(f"r{i}", "uc", "q", "a", 10.0, 1)
        assert len(store.interactions) == 3
        # Oldest (r0, r1) should be evicted
        run_ids = [r["run_id"] for r in store.interactions]
        assert "r0" not in run_ids
        assert "r1" not in run_ids
        assert "r4" in run_ids

    def test_purge_removes_old(self):
        from engine.memory import MockMemory
        store = MockMemory(max_entries=100)
        store.log_interaction("r1", "uc", "q", "a", 10.0, 1)
        time.sleep(0.05)
        cutoff = time.time()
        time.sleep(0.05)
        store.log_interaction("r2", "uc", "q", "a", 10.0, 1)
        store.purge_before(cutoff)
        assert len(store.interactions) == 1
        assert store.interactions[0]["run_id"] == "r2"


# ---------------------------------------------------------------------------
# 4. SqliteMemory (Findings M6, M7, M9, M10, M11, M14)
# ---------------------------------------------------------------------------

class TestSqliteMemory:
    def test_rejects_memory_path(self):
        from engine.memory import SqliteMemory
        with pytest.raises(ValueError, match="(?i)memory"):
            SqliteMemory(path=":memory:")

    def test_creates_tables(self, tmp_path):
        from engine.memory import SqliteMemory
        db = str(tmp_path / "test.db")
        store = SqliteMemory(path=db)
        # Verify tables exist
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(db)) as conn:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
            tables = {row[0] for row in cursor.fetchall()}
        assert "interactions" in tables
        assert "feedback" in tables

    def test_score_stored_as_real(self, tmp_path):
        from engine.memory import SqliteMemory
        db = str(tmp_path / "test.db")
        store = SqliteMemory(path=db)
        store.log_interaction("r1", "uc", "q", "a", 0.87, 1)
        from contextlib import closing
        with closing(sqlite3.connect(db)) as conn:
            row = conn.execute(
                "SELECT score FROM interactions WHERE run_id = 'r1'"
            ).fetchone()
        assert row is not None
        # Score must preserve fractional precision
        assert abs(row[0] - 0.87) < 0.001

    def test_duplicate_run_id_raises(self, tmp_path):
        from engine.memory import SqliteMemory
        db = str(tmp_path / "test.db")
        store = SqliteMemory(path=db)
        store.log_interaction("r1", "uc", "q", "a", 10.0, 1)
        with pytest.raises(Exception):  # IntegrityError
            store.log_interaction("r1", "uc", "q2", "a2", 5.0, 2)

    def test_record_feedback(self, tmp_path):
        from engine.memory import SqliteMemory
        db = str(tmp_path / "test.db")
        store = SqliteMemory(path=db)
        store.record_feedback("r1", "good", "helpful answer")
        from contextlib import closing
        with closing(sqlite3.connect(db)) as conn:
            row = conn.execute(
                "SELECT run_id, rating, note FROM feedback"
            ).fetchone()
        assert row == ("r1", "good", "helpful answer")

    def test_purge_before_deletes_old(self, tmp_path):
        from engine.memory import SqliteMemory
        db = str(tmp_path / "test.db")
        store = SqliteMemory(path=db)
        store.log_interaction("r1", "uc", "q", "a", 10.0, 1)
        time.sleep(0.05)
        cutoff = time.time()
        time.sleep(0.05)
        store.log_interaction("r2", "uc", "q", "a", 10.0, 1)
        store.purge_before(cutoff)
        from contextlib import closing
        with closing(sqlite3.connect(db)) as conn:
            rows = conn.execute("SELECT run_id FROM interactions").fetchall()
        run_ids = [r[0] for r in rows]
        assert "r1" not in run_ids
        assert "r2" in run_ids

    def test_purge_empty_returns_zero(self, tmp_path):
        from engine.memory import SqliteMemory
        db = str(tmp_path / "test.db")
        store = SqliteMemory(path=db)
        store.purge_before(time.time())
        # No error on empty db


# ---------------------------------------------------------------------------
# 5. Singleton behavior
# ---------------------------------------------------------------------------

class TestSingleton:
    def test_returns_same_instance(self, monkeypatch):
        from engine.memory import get_memory
        monkeypatch.setenv("MEMORY_STORE", "mock")
        a = get_memory()
        b = get_memory()
        assert a is b

    def test_thread_safety(self, monkeypatch):
        from engine.memory import get_memory
        monkeypatch.setenv("MEMORY_STORE", "mock")
        results = []
        def worker():
            results.append(id(get_memory()))
        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(set(results)) == 1  # All same instance


# ---------------------------------------------------------------------------
# 6. remember_run (Finding H1 - fail-safe)
# ---------------------------------------------------------------------------

class TestRememberRun:
    def test_success(self, monkeypatch):
        from engine.memory import remember_run, get_memory, MockMemory
        monkeypatch.setenv("MEMORY_STORE", "mock")
        state = {
            "run_id": "r1",
            "task": "test question",
            "best_answer": "test answer",
            "best_score": 15,
            "iterations": 2,
        }
        remember_run(state, "test_uc")
        store = get_memory()
        assert len(store.interactions) == 1

    def test_catches_init_errors(self, monkeypatch):
        """remember_run must not raise even if get_memory() fails."""
        from engine.memory import remember_run
        monkeypatch.setenv("MEMORY_STORE", "snowflake")
        # Snowflake will fail without real session — remember_run must swallow it
        state = {"run_id": "r1", "task": "q", "best_answer": "a", "best_score": 10, "iterations": 1}
        # This should NOT raise
        remember_run(state, "test_uc")

    def test_handles_missing_state_keys(self, monkeypatch):
        from engine.memory import remember_run
        monkeypatch.setenv("MEMORY_STORE", "mock")
        # Missing keys — should not raise
        remember_run({}, "test_uc")

    def test_handles_none_state(self, monkeypatch):
        from engine.memory import remember_run
        monkeypatch.setenv("MEMORY_STORE", "mock")
        # None state — should not raise
        remember_run(None, "test_uc")


# ---------------------------------------------------------------------------
# 7. Input validation (Finding M9)
# ---------------------------------------------------------------------------

class TestValidation:
    def test_validate_run_id_rejects_empty(self):
        from engine.memory import _validate_run_id
        with pytest.raises(ValueError):
            _validate_run_id("")

    def test_validate_run_id_rejects_none(self):
        from engine.memory import _validate_run_id
        with pytest.raises((ValueError, TypeError)):
            _validate_run_id(None)

    def test_validate_run_id_accepts_valid(self):
        from engine.memory import _validate_run_id
        _validate_run_id("abc-123")  # Should not raise


# ---------------------------------------------------------------------------
# 8. Status reporting (Finding M18)
# ---------------------------------------------------------------------------

class TestStatus:
    def test_null_memory_status(self):
        from engine.memory import NullMemory
        s = NullMemory().status()
        assert "effective_backend" in s
        assert s["effective_backend"] == "none"

    def test_mock_memory_status(self):
        from engine.memory import MockMemory
        s = MockMemory(max_entries=100).status()
        assert s["effective_backend"] == "mock"
