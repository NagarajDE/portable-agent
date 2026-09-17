"""R2 -- GenieTool (SQL_TOOL=genie), the Databricks mirror of CortexAnalystTool, exercised against a fake
`databricks.sdk` WorkspaceClient (the `client=` seam): SQL attachment -> rows text; clarification -> NO_DATA
with the hint; read-only backstop; row cap; last_sql capture; the optional metric-view discovery
capabilities; and the config guard rail. No SDK, no creds, no network."""
from types import SimpleNamespace as NS

import pytest

from engine.sql_tool import GenieTool, is_no_data, data_hint


def _stmt(cols, rows, state="SUCCEEDED", truncated=False, next_chunk=None):
    return NS(status=NS(state=state, error=None),
              manifest=NS(schema=NS(columns=[NS(name=c) for c in cols]), truncated=truncated),
              result=NS(data_array=rows, next_chunk_index=next_chunk))


class _FakeGenie:
    def __init__(self, *, sql=None, text=None, status="COMPLETED", stmt=None):
        self.sql, self.text, self.status, self.stmt, self.asked = sql, text, status, stmt, []

    def start_conversation_and_wait(self, space_id, content, timeout=None):
        self.asked.append((space_id, content))
        atts = []
        if self.sql:
            atts.append(NS(attachment_id="att-1", query=NS(query=self.sql, description="d"), text=None))
        if self.text:
            atts.append(NS(attachment_id="att-2", query=None, text=NS(content=self.text)))
        return NS(status=self.status, conversation_id="c-1", message_id="m-1", attachments=atts,
                  error=NS(error="boom") if self.status == "FAILED" else None)

    def get_message_attachment_query_result(self, space_id, conversation_id, message_id, attachment_id):
        assert (space_id, conversation_id, message_id, attachment_id) == ("sp", "c-1", "m-1", "att-1")
        return NS(statement_response=self.stmt)


class _FakeStatements:
    """statement_execution double: maps a statement (prefix match) to a (cols, rows) result or an error."""
    def __init__(self, table):
        self.table, self.ran = table, []

    def execute_statement(self, statement, warehouse_id, wait_timeout=None, on_wait_timeout=None, row_limit=None):
        self.ran.append(statement)
        for prefix, res in self.table.items():
            if statement.startswith(prefix):
                if isinstance(res, Exception):
                    raise res
                cols, rows = res
                return _stmt(cols, rows)
        raise RuntimeError(f"no fake result for {statement!r}")


def _client(genie, statements=None):
    return NS(genie=genie, statement_execution=statements or _FakeStatements({}))


_BLOCK = {"genie_space": "sp", "metric_view": "main.gold.sales"}


# --- ask(): the text->SQL->rows path -------------------------------------------------------------------------
def test_ask_returns_rows_text_and_captures_last_sql():
    genie = _FakeGenie(sql="SELECT region, SUM(x) FROM t GROUP BY 1",
                       stmt=_stmt(["REGION", "TOTAL"], [["EMEA", "10"], ["APAC", "7"]]))
    tool = GenieTool(_BLOCK, client=_client(genie))
    out = tool.ask("totals by region")
    assert out == "REGION | TOTAL\nEMEA | 10\nAPAC | 7"
    assert genie.asked == [("sp", "totals by region")]
    assert tool.last_sql == "SELECT region, SUM(x) FROM t GROUP BY 1"     # for auto value binding


def test_ask_zero_rows_is_the_no_data_sentinel():
    genie = _FakeGenie(sql="SELECT 1", stmt=_stmt(["A"], []))
    assert is_no_data(GenieTool(_BLOCK, client=_client(genie)).ask("q"))


def test_ask_clarification_without_sql_is_no_data_with_hint():
    genie = _FakeGenie(text="Which fiscal year do you mean?")
    out = GenieTool(_BLOCK, client=_client(genie)).ask("q")
    assert is_no_data(out) and data_hint(out) == "Which fiscal year do you mean?"


def test_ask_refuses_non_select_sql_from_genie():
    genie = _FakeGenie(sql="DELETE FROM t", stmt=_stmt(["A"], [["1"]]))
    with pytest.raises(ValueError):                       # the read-only backstop, same as Cortex
        GenieTool(_BLOCK, client=_client(genie)).ask("q")


def test_ask_failed_message_raises_with_detail():
    genie = _FakeGenie(sql="SELECT 1", status="FAILED", stmt=_stmt(["A"], [["1"]]))
    with pytest.raises(RuntimeError, match="FAILED"):
        GenieTool(_BLOCK, client=_client(genie)).ask("q")


def test_ask_failed_statement_raises():
    genie = _FakeGenie(sql="SELECT 1", stmt=_stmt(["A"], [], state="FAILED"))
    with pytest.raises(RuntimeError, match="Genie query FAILED"):
        GenieTool(_BLOCK, client=_client(genie)).ask("q")


def test_ask_caps_rows_with_a_visible_note(monkeypatch):
    monkeypatch.setenv("SQL_MAX_ROWS", "2")
    genie = _FakeGenie(sql="SELECT 1", stmt=_stmt(["N"], [["1"], ["2"], ["3"]]))
    out = GenieTool(_BLOCK, client=_client(genie)).ask("q")
    assert out == "N\n1\n2\n... (additional rows omitted)"


def test_ask_marks_server_side_truncation():
    genie = _FakeGenie(sql="SELECT 1", stmt=_stmt(["N"], [["1"]], truncated=True))
    assert GenieTool(_BLOCK, client=_client(genie)).ask("q").endswith("(additional rows omitted)")


# --- construction / guard rails --------------------------------------------------------------------------------
def test_requires_a_genie_space_before_any_sdk():
    with pytest.raises(KeyError, match="genie_space"):
        GenieTool({"metric_view": "m.s.v"}, client=object())   # a metric view alone is not addressable


def test_genie_space_only_is_enough_and_discovery_degrades_to_nothing():
    tool = GenieTool({"genie_space": "sp"}, client=_client(_FakeGenie()))
    assert tool.dimension_paths() == [] and tool.distinct_values(["V.C"]) == {}   # no metric view -> no-op


# --- optional discovery (metric_view + DATABRICKS_WAREHOUSE_ID) -> the binding mechanism works on Genie too ----
def test_dimension_paths_and_distinct_values_from_the_metric_view(monkeypatch):
    monkeypatch.setenv("DATABRICKS_WAREHOUSE_ID", "wh1")
    st = _FakeStatements({
        "DESCRIBE TABLE main.gold.sales": (["col_name", "data_type", "comment"],
                                          [["region", "string", ""], ["status", "string", ""],
                                           ["revenue", "decimal", ""], ["# Partition", "", ""]]),
        "SELECT `status` FROM main.gold.sales": (["status"], [["Executed"], ["Approved"], [None], ["Executed"]]),
        "SELECT `revenue` FROM main.gold.sales": RuntimeError("MEASURE() required"),   # a measure column
    })
    tool = GenieTool(_BLOCK, client=_client(_FakeGenie(), st))
    assert tool.dimension_paths() == ["sales.region", "sales.status", "sales.revenue"]
    assert tool.dimension_paths() == ["sales.region", "sales.status", "sales.revenue"]   # cached
    assert st.ran.count("DESCRIBE TABLE main.gold.sales") == 1
    vals = tool.distinct_values(["sales.status", "sales.revenue", "bad path!"])
    assert vals == {"sales.status": ["Executed", "Approved"]}        # measure skipped, NULL/dupes dropped, bad id skipped


def test_discovery_without_a_warehouse_is_a_silent_no_op():
    st = _FakeStatements({})
    tool = GenieTool(_BLOCK, client=_client(_FakeGenie(), st))
    assert tool.dimension_paths() == [] and tool.distinct_values(["sales.status"]) == {} and st.ran == []


# --- the tool rides the standard retrieval path (dispatch + binding) unchanged --------------------------------
def test_genie_through_the_sql_retriever_recovers_values_on_a_blank(monkeypatch):
    monkeypatch.setenv("DATABRICKS_WAREHOUSE_ID", "wh1")
    from engine.retrieval import SqlRetriever
    from engine.binding import RECOVERY_HEADER
    genie = _FakeGenie(sql="SELECT * FROM s WHERE status = 'Active'", stmt=_stmt(["A"], []))
    st = _FakeStatements({"DESCRIBE TABLE main.gold.sales": (["col_name"], [["status"]]),
                          "SELECT `status` FROM main.gold.sales": (["status"], [["Executed"], ["Approved"]])})
    r = SqlRetriever(GenieTool(_BLOCK, client=_client(genie, st)))
    assert r.retrieve("active totals").empty
    block = r.recovery_block()
    assert RECOVERY_HEADER in block and "status: Executed, Approved" in block


def test_missing_databricks_sdk_is_an_actionable_error(monkeypatch):
    """IMPROVEMENT 3 (live): SQL_TOOL=genie without databricks-sdk must name Genie + the package, not die
    with a bare ModuleNotFoundError."""
    import sys
    import engine.llm_client as L
    monkeypatch.setitem(sys.modules, "databricks", None)          # simulate an env without the SDK
    monkeypatch.setitem(sys.modules, "databricks.sdk", None)
    monkeypatch.setattr(L, "_ws_client", None)
    with pytest.raises(ImportError) as e:
        GenieTool({"genie_space": "sp"})                          # no injected client -> real SDK path
    msg = str(e.value)
    assert "Genie" in msg and "pip install databricks-sdk" in msg and "SQL_TOOL=genie" in msg


def test_workspace_client_normalizes_host_and_is_memoized(monkeypatch):
    import sys, types
    import engine.llm_client as L
    seen = []
    sdk = types.ModuleType("databricks.sdk")
    sdk.WorkspaceClient = lambda host=None: seen.append(host) or object()
    monkeypatch.setitem(sys.modules, "databricks", types.ModuleType("databricks"))
    monkeypatch.setitem(sys.modules, "databricks.sdk", sdk)
    monkeypatch.setattr(L, "_ws_client", None)
    monkeypatch.setenv("DATABRICKS_HOST", "https://w.cloud.databricks.com/serving-endpoints")
    a, b = L.databricks_workspace_client(), L.databricks_workspace_client()
    assert a is b and seen == ["https://w.cloud.databricks.com"]     # path stripped, ONE client
    monkeypatch.setattr(L, "_ws_client", None)
