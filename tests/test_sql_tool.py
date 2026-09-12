"""Tests for the read-only backstop on model-generated SQL (defense-in-depth)."""
import pytest

import engine.llm_client as _llm
from engine.sql_tool import _ensure_read_only, CortexAnalystTool, get_sql_tool


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


# NB1: a real 2nd statement must NOT be hidden by a comment marker INSIDE a string literal,
# nor by string/comment markers interacting across sequential passes.
@pytest.mark.parametrize("bad", [
    "SELECT '-- '; DROP TABLE t",                       # '--' inside the string once ate the ';'
    "SELECT $$'$$; DROP TABLE t; SELECT $$'$$",         # dollar-quote vs single-quote interaction
    "SELECT '/*'; DROP TABLE t",                        # block-comment marker inside a string
    r"SELECT '\''; DROP TABLE t",                       # backslash-escaped quote (Snowflake \')
    'SELECT 1 AS "\'"; DROP TABLE t',                   # apostrophe inside a "quoted identifier"
])
def test_rejects_hidden_second_statement(bad):
    with pytest.raises(ValueError):
        _ensure_read_only(bad)


# ...and the corresponding legitimate single statements must still be ACCEPTED (no false positive)
@pytest.mark.parametrize("ok", [
    r"SELECT '\'' AS q",                                # a string that is just an escaped quote
    'SELECT 1 AS "a;b"',                                # ';' inside a delimited identifier is not a separator
    'SELECT 1 AS "a""b"',                               # doubled "" inside a delimited identifier
])
def test_allows_legit_escaped_and_quoted_identifiers(ok):
    assert _ensure_read_only(ok)


# --- CortexAnalystTool: the semantic layer comes from the PACK (a dict), NEVER env ----------
@pytest.fixture
def _no_snowpark(monkeypatch):
    """Stub the Snowpark session so __init__ doesn't need live creds."""
    monkeypatch.setattr(_llm, "snowpark_session", lambda: object())


def test_cortex_prefers_semantic_view(_no_snowpark):
    # both keys present -> view wins (mirrors the prior precedence, now on the passed-in block)
    tool = CortexAnalystTool({"view": "DB.SCHEMA.MY_VIEW", "model_file": "@DB.SCHEMA.STAGE/m.yaml"})
    assert tool._semantic == {"semantic_view": "DB.SCHEMA.MY_VIEW"}


def test_cortex_falls_back_to_stage_yaml(_no_snowpark):
    assert CortexAnalystTool({"model_file": "@DB.SCHEMA.STAGE/m.yaml"})._semantic == \
        {"semantic_model_file": "@DB.SCHEMA.STAGE/m.yaml"}


def test_cortex_ignores_env_semantic_layer(monkeypatch, _no_snowpark):
    # engine/ must NOT read env for functional config: even with env set, the PACK block wins.
    monkeypatch.setenv("CORTEX_SEMANTIC_VIEW", "ENV.DB.SHOULD_BE_IGNORED")
    monkeypatch.setenv("CORTEX_SEMANTIC_MODEL", "@ENV/ignored.yaml")
    assert CortexAnalystTool({"view": "PACK.DB.MY_VIEW"})._semantic == {"semantic_view": "PACK.DB.MY_VIEW"}


def test_cortex_requires_a_semantic_layer_before_session(monkeypatch):
    # NO snowpark stub on purpose: the semantic-layer check must fail FIRST, so a missing
    # block raises the clear KeyError even if a session could never be opened (ordering).
    def _boom():
        raise RuntimeError("session should not be attempted before the semantic-layer check")
    monkeypatch.setattr(_llm, "snowpark_session", _boom)
    with pytest.raises(KeyError):
        CortexAnalystTool({})            # empty block -> neither view nor model_file


# --- get_sql_tool: platform selection + two-sided guard rails -------------------------------
def test_get_sql_tool_mock_ignores_semantic_layer(monkeypatch):
    # mock is a fixture path: even with no layer, a mock run must work (non-AI+BI packs are fine).
    monkeypatch.setenv("SQL_TOOL", "mock")
    tool = get_sql_tool("dq_qals", "mock", semantic_layer=None)
    assert hasattr(tool, "ask")          # MockSQLTool from the pack fixtures


def test_get_sql_tool_cortex_selects_snowflake_block(monkeypatch, _no_snowpark):
    monkeypatch.setenv("SQL_TOOL", "cortex")
    tool = get_sql_tool("any_pack", "mock", semantic_layer={"snowflake": {"view": "DB.S.V"}})
    assert tool._semantic == {"semantic_view": "DB.S.V"}


def test_get_sql_tool_cortex_missing_layer_is_guard_railed(monkeypatch):
    monkeypatch.setenv("SQL_TOOL", "cortex")
    with pytest.raises(KeyError):        # an Analyst pack that declared no semantic layer
        get_sql_tool("some_pack", "mock", semantic_layer=None)


def test_get_sql_tool_genie_selects_databricks_block(monkeypatch):
    # GenieTool is a stub that raises NotImplementedError at construction; a present databricks
    # block gets PAST the guard rail (reaching the stub), which proves platform selection works.
    monkeypatch.setenv("SQL_TOOL", "genie")
    with pytest.raises(NotImplementedError):
        get_sql_tool("p", "mock", semantic_layer={"databricks": {"metric_view": "m.s.v"}})


def test_get_sql_tool_genie_missing_block_is_guard_railed(monkeypatch):
    monkeypatch.setenv("SQL_TOOL", "genie")
    with pytest.raises(KeyError):        # a snowflake-only pack has no databricks block
        get_sql_tool("p", "mock", semantic_layer={"snowflake": {"view": "DB.S.V"}})
