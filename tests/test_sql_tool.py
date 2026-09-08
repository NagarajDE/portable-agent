"""Tests for the read-only backstop on model-generated SQL (defense-in-depth)."""
import pytest

from engine.sql_tool import _ensure_read_only


def test_allows_select():
    assert _ensure_read_only("SELECT 1").upper().startswith("SELECT")


def test_allows_with_cte():
    assert _ensure_read_only("WITH t AS (SELECT 1) SELECT * FROM t")


def test_strips_trailing_semicolon():
    assert _ensure_read_only("SELECT 1;") == "SELECT 1"


@pytest.mark.parametrize("bad", [
    "DELETE FROM t",
    "DROP TABLE t",
    "UPDATE t SET x = 1",
    "INSERT INTO t VALUES (1)",
    "MERGE INTO t USING s ON t.id = s.id",
    "SELECT 1; DROP TABLE t",          # multi-statement
    "TRUNCATE TABLE t",
])
def test_rejects_writes_and_multistatement(bad):
    with pytest.raises(ValueError):
        _ensure_read_only(bad)
