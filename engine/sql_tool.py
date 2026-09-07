"""
ENGINE / text-to-SQL as a swappable TOOL.  GENERIC -- the interface and the
real adapters live here; the MOCK rows live in each use-case's fixtures.py.

Switch with:  SQL_TOOL=mock|cortex|genie
"""
from __future__ import annotations
import os
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
        return _rows_to_text(self._s.sql(sql).collect())


# Databricks Genie -- text-to-SQL over Unity Catalog (managed MCP tool once deployed).
class GenieTool:
    def __init__(self):
        from databricks.sdk import WorkspaceClient
        self._w = WorkspaceClient()
        self._space_id = os.environ["GENIE_SPACE_ID"]

    def ask(self, question: str) -> str:
        # Start/continue a Genie conversation, poll for SQL result, format rows.
        raise NotImplementedError("wire Genie conversation API (or MCP tool) here")


def get_sql_tool(use_case: str | None = None) -> SQLTool:
    tool = os.getenv("SQL_TOOL", "mock").lower()
    if tool == "mock":                                   # mock is a use-case fixture
        mod = importlib.import_module(f"usecases.{use_case}.fixtures")
        return mod.MockSQLTool()
    return {"cortex": CortexAnalystTool, "genie": GenieTool}[tool]()
