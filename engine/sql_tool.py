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


def _rows_to_text(rows, max_rows: int = 100) -> str:
    """Format Snowpark result rows into a compact text block for the LLM to read."""
    if not rows:
        return "No rows."
    dicts = [r.as_dict() for r in rows[:max_rows]]
    cols = list(dicts[0].keys())
    lines = [" | ".join(cols)] + [" | ".join(str(d.get(c)) for c in cols) for d in dicts]
    if len(rows) > max_rows:
        lines.append(f"... ({len(rows) - max_rows} more rows)")
    return "\n".join(lines)


def _blank_strings_and_comments(sql: str) -> str:
    """Return sql with string literals and comments removed, in ONE left-to-right pass that
    tracks lexical state (bare / '…' / $$…$$ / --line / /*block*/). A single scanner -- unlike
    sequential regex passes -- can't be fooled by a comment marker inside a string or a quote
    inside a comment (NB1): e.g. `SELECT '-- '; DROP TABLE t` no longer has its ';' hidden by an
    over-eager comment strip. Used only to test for a real statement separator."""
    out, i, n = [], 0, len(sql)
    while i < n:
        two = sql[i:i + 2]
        if two == "--":                                  # line comment -> to EOL
            j = sql.find("\n", i)
            i = n if j == -1 else j
        elif two == "/*":                                # block comment -> to */
            j = sql.find("*/", i + 2)
            i = n if j == -1 else j + 2
        elif two == "$$":                                # dollar-quoted string -> to next $$
            j = sql.find("$$", i + 2)
            i = n if j == -1 else j + 2
        elif sql[i] == "'":                              # single-quoted string ('' escapes a quote)
            i += 1
            while i < n:
                if sql[i] == "'":
                    if sql[i + 1:i + 2] == "'":
                        i += 2                            # doubled quote -> stay in string
                        continue
                    i += 1
                    break
                i += 1
        else:
            out.append(sql[i])
            i += 1
    return "".join(out)


def _ensure_read_only(sql: str) -> str:
    """Defense-in-depth HEURISTIC, not a security boundary: reject obvious writes and
    multi-statement SQL from the model. The REAL control is granting the service role
    SELECT-only (see the deploy guide) — a determined bypass of a heuristic is possible;
    a read-only grant is not. Tolerates leading comments and semicolons inside string
    literals so it doesn't reject legitimate SELECTs."""
    stripped = sql.strip()
    while True:                                          # drop leading -- and /* */ comments
        s2 = re.sub(r"^\s*--[^\n]*\n?", "", stripped)
        s2 = re.sub(r"^\s*/\*.*?\*/\s*", "", s2, flags=re.DOTALL)
        if s2 == stripped:
            break
        stripped = s2
    stripped = stripped.rstrip(";").strip()
    if not re.match(r"(?is)^(select|with)\b", stripped):
        raise ValueError("refusing non-SELECT SQL from the model (read-only heuristic)")
    # Multi-statement check: blank strings/comments in one pass so a ';' inside them isn't a
    # false positive, and (NB1) a ';' outside them can't be hidden by a naive comment strip.
    if ";" in _blank_strings_and_comments(stripped):
        raise ValueError("refusing multi-statement SQL from the model")
    return stripped


# Snowflake Cortex Analyst -- text-to-SQL over a semantic model (REST), SQL run via Snowpark.
class CortexAnalystTool:
    def __init__(self):
        # Resolve the semantic layer FIRST, so a missing/misconfigured layer fails with a clear
        # message BEFORE we open a Snowpark session (which needs creds + network and would
        # otherwise mask this error). Cortex Analyst accepts EITHER a native Semantic View (an
        # object already in the account) OR a semantic model YAML on a stage; prefer an existing
        # view if set; exactly one is required. This is the only place the two forms differ.
        view = (os.getenv("CORTEX_SEMANTIC_VIEW") or "").strip()     # DB.SCHEMA.MY_SEMANTIC_VIEW
        model = (os.getenv("CORTEX_SEMANTIC_MODEL") or "").strip()   # @db.schema.stage/model.yaml
        if view:
            self._semantic = {"semantic_view": view}
        elif model:
            self._semantic = {"semantic_model_file": model}
        else:
            raise KeyError("SQL_TOOL=cortex needs a semantic layer: set CORTEX_SEMANTIC_VIEW "
                           "(a native Semantic View) or CORTEX_SEMANTIC_MODEL (a stage YAML).")
        from engine.llm_client import snowpark_session
        self._s = snowpark_session()                                 # runs the generated SQL

    def ask(self, question: str) -> str:
        import requests
        from engine.llm_client import snowflake_rest_base, snowflake_bearer_headers
        body = {"messages": [{"role": "user",
                              "content": [{"type": "text", "text": question}]}],
                **self._semantic}
        resp = requests.post(f"{snowflake_rest_base()}/api/v2/cortex/analyst/message",
                             headers=snowflake_bearer_headers(), json=body, timeout=60)
        resp.raise_for_status()
        content = resp.json().get("message", {}).get("content", [])
        sql = next((c["statement"] for c in content if c.get("type") == "sql"), None)
        if not sql:                                                  # ambiguous Q -> return the text
            texts = [c.get("text", "") for c in content if c.get("type") == "text"]
            return "\n".join(t for t in texts if t) or "Cortex Analyst returned no SQL."
        sql = _ensure_read_only(sql)                                 # backstop: SELECT-only, single statement
        max_rows = max(1, int(os.getenv("SQL_MAX_ROWS", "100")))     # how many result rows the LLM sees
        timeout = max(1, int(os.getenv("SQL_TIMEOUT_SECONDS", "30")))  # >=1: timeout=0 must not remove the bound
        rows = self._s.sql(sql).limit(max_rows + 1).collect(         # cap BEFORE collect() to bound memory
            statement_params={"STATEMENT_TIMEOUT_IN_SECONDS": str(timeout)})
        text = _rows_to_text(rows[:max_rows], max_rows)
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
