"""
ENGINE / text-to-SQL as a swappable TOOL.  GENERIC -- the interface and the
real adapters live here; the MOCK rows live in each use-case's fixtures.py.

Switch with:  SQL_TOOL=mock|cortex|genie
"""
from __future__ import annotations
import os
import re
import importlib
from typing import Protocol, runtime_checkable


@runtime_checkable
class SQLTool(Protocol):
    def ask(self, question: str) -> str: ...      # returns rows as text


def _rows_to_text(rows, max_rows: int = 50) -> str:
    """Format Snowpark result rows into a compact text block for the LLM to read."""
    if not rows:
        return "No rows."
    dicts = [r.as_dict() for r in rows[:max_rows]]
    cols = list(dicts[0].keys())
    lines = [" | ".join(cols)] + [" | ".join(str(d.get(c)) for c in cols) for d in dicts]
    if len(rows) > max_rows:
        lines.append(f"... ({len(rows) - max_rows} more rows)")
    return "\n".join(lines)


def _ensure_read_only(sql: str) -> str:
    """Backstop before executing model-generated SQL: exactly one statement, and it must be
    a read-only SELECT/WITH. The PRIMARY control is granting the service role SELECT-only
    (see the deploy guide); this just stops an obvious write/multi-statement slipping through."""
    stripped = sql.strip().rstrip(";").strip()
    if ";" in stripped:
        raise ValueError("refusing multi-statement SQL from the model")
    if not re.match(r"(?is)^\s*(select|with)\b", stripped):
        raise ValueError("refusing non-SELECT SQL from the model (read-only only)")
    return stripped


# Snowflake Cortex Analyst -- text-to-SQL over a semantic model (REST), SQL run via Snowpark.
class CortexAnalystTool:
    def __init__(self):
        from engine.llm_client import snowpark_session
        self._s = snowpark_session()                                 # runs the generated SQL
        self._semantic_model = os.environ["CORTEX_SEMANTIC_MODEL"]   # @db.schema.stage/model.yaml

    def ask(self, question: str) -> str:
        import requests
        from engine.llm_client import snowflake_rest_base, snowflake_bearer_headers
        body = {"messages": [{"role": "user",
                              "content": [{"type": "text", "text": question}]}],
                "semantic_model_file": self._semantic_model}
        resp = requests.post(f"{snowflake_rest_base()}/api/v2/cortex/analyst/message",
                             headers=snowflake_bearer_headers(), json=body, timeout=60)
        resp.raise_for_status()
        content = resp.json().get("message", {}).get("content", [])
        sql = next((c["statement"] for c in content if c.get("type") == "sql"), None)
        if not sql:                                                  # ambiguous Q -> return the text
            texts = [c.get("text", "") for c in content if c.get("type") == "text"]
            return "\n".join(t for t in texts if t) or "Cortex Analyst returned no SQL."
        sql = _ensure_read_only(sql)                                 # backstop: SELECT-only, single statement
        max_rows = 50                                                # cap BEFORE collect() to bound memory
        timeout = int(os.getenv("SQL_TIMEOUT_SECONDS", "30"))        # bound warehouse time for a runaway query
        rows = self._s.sql(sql).limit(max_rows + 1).collect(
            statement_params={"STATEMENT_TIMEOUT_IN_SECONDS": str(timeout)})
        text = _rows_to_text(rows[:max_rows])
        return text + ("\n... (additional rows omitted)" if len(rows) > max_rows else "")


# Databricks Genie -- text-to-SQL over Unity Catalog (managed MCP tool once deployed).
# NOT YET WIRED. Fail fast at construction with an actionable message rather than a late,
# opaque error mid-request. To run on Databricks today, use SQL_TOOL=mock until this is
# implemented (mirror CortexAnalystTool: start/continue a Genie conversation, run the SQL).
class GenieTool:
    def __init__(self):
        raise NotImplementedError(
            "SQL_TOOL=genie is not implemented yet. Use SQL_TOOL=mock on Databricks for now, "
            "or wire the Genie conversation API here (see engine/sql_tool.py).")

    def ask(self, question: str) -> str:                 # pragma: no cover - unreachable until wired
        raise NotImplementedError


def get_sql_tool(use_case: str | None = None, default: str = "mock") -> SQLTool:
    # env SQL_TOOL wins; else the pack's default_sql_tool (passed in) -- no os.environ mutation,
    # so one graph's default can't leak to the next in the same process.
    tool = (os.getenv("SQL_TOOL") or default).strip().lower()
    if tool == "mock":                                   # mock is a use-case fixture
        mod = importlib.import_module(f"usecases.{use_case}.fixtures")
        return mod.MockSQLTool()
    if tool not in ("cortex", "genie"):
        raise ValueError(f"unknown SQL_TOOL: {tool!r} (expected mock|cortex|genie)")
    return {"cortex": CortexAnalystTool, "genie": GenieTool}[tool]()
