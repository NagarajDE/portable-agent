"""Tests for the read-only backstop on model-generated SQL (defense-in-depth)."""
import pytest

import engine.llm_client as _llm
from engine.sql_tool import _ensure_read_only, CortexAnalystTool


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


# N4: legitimate SELECTs must NOT be rejected as false positives
def test_allows_semicolon_inside_string_literal():
    assert _ensure_read_only("SELECT ';' AS delimiter")


def test_allows_leading_line_comment():
    assert _ensure_read_only("-- pick one\nSELECT 1")


def test_allows_leading_block_comment():
    assert _ensure_read_only("/* header */ SELECT 1")


def test_allows_dollar_quoted_semicolon():
    assert _ensure_read_only("SELECT $$a;b$$ AS x")     # ';' inside $$...$$ is not a 2nd statement


def test_allows_inline_line_comment_with_semicolon():
    assert _ensure_read_only("SELECT 1 -- ; not a statement\n")


# --- CortexAnalystTool: semantic layer selection (view OR stage YAML) -------
@pytest.fixture
def _no_snowpark(monkeypatch):
    """Stub the Snowpark session so __init__ doesn't need live creds."""
    monkeypatch.setattr(_llm, "snowpark_session", lambda: object())


def test_cortex_prefers_semantic_view(monkeypatch, _no_snowpark):
    monkeypatch.setenv("CORTEX_SEMANTIC_VIEW", "DB.SCHEMA.MY_VIEW")
    monkeypatch.setenv("CORTEX_SEMANTIC_MODEL", "@DB.SCHEMA.STAGE/m.yaml")  # both set -> view wins
    assert CortexAnalystTool()._semantic == {"semantic_view": "DB.SCHEMA.MY_VIEW"}


def test_cortex_falls_back_to_stage_yaml(monkeypatch, _no_snowpark):
    monkeypatch.delenv("CORTEX_SEMANTIC_VIEW", raising=False)
    monkeypatch.setenv("CORTEX_SEMANTIC_MODEL", "@DB.SCHEMA.STAGE/m.yaml")
    assert CortexAnalystTool()._semantic == {"semantic_model_file": "@DB.SCHEMA.STAGE/m.yaml"}


def test_cortex_requires_a_semantic_layer(monkeypatch, _no_snowpark):
    monkeypatch.delenv("CORTEX_SEMANTIC_VIEW", raising=False)
    monkeypatch.delenv("CORTEX_SEMANTIC_MODEL", raising=False)
    with pytest.raises(KeyError):
        CortexAnalystTool()
