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


# A retrieval that came back with NO usable rows is marked with this sentinel rather than flattened
# into benign-looking prose ("No rows." / a clarification), so the loop can tell "answered from data"
# from "no data" DETERMINISTICALLY -- the SCORE must never be what encodes groundedness (a probabilistic
# judge can't be trusted to notice). is_no_data()/data_hint() read it back; the hint carries any tool
# clarification, which the loop uses to reformulate the question and re-query (see engine/graph.py).
NO_DATA = "__PA_NO_DATA__"


def no_data(hint: str = "") -> str:
    """Mark a blank retrieval, optionally carrying a human/reformulation hint (why nothing came back)."""
    hint = (hint or "").strip()
    return NO_DATA if not hint else f"{NO_DATA}\n{hint}"


def is_no_data(data) -> bool:
    return isinstance(data, str) and data.startswith(NO_DATA)


def data_hint(data) -> str:
    """The clarification text a no-data sentinel carries ('' if none / not a sentinel)."""
    return data[len(NO_DATA):].strip() if is_no_data(data) else ""


def looks_all_zero(data) -> bool:
    """True when a result is ONE data row whose every cell is 0 / NULL / blank -- structurally a row
    (so is_no_data() is False) yet carrying no information: COUNT(*)=0, SUM(...)=NULL, typically a
    filter or status label that matched nothing. Deliberately narrow: header + EXACTLY one row.

    This is INDISTINGUISHABLE from a genuine zero ("we really do have 0 active contracts"), so the
    loop only honors it when a pack opts in with `zero_is_no_data: true` -- see engine/graph.py."""
    if not isinstance(data, str) or is_no_data(data):
        return False
    lines = [ln for ln in data.strip().splitlines() if ln.strip()]
    if len(lines) != 2:                              # header + one data row, nothing else
        return False

    def empty(cell: str) -> bool:
        c = cell.strip().strip("$%").replace(",", "").lower()
        if c in ("", "none", "null", "nan", "-"):
            return True
        try:
            return float(c) == 0.0                   # 0, 0.0, -0, 0e0 ...
        except ValueError:
            return False                             # any real label/value -> informative

    return all(empty(c) for c in lines[1].split("|"))


def _table_to_text(cols, rows) -> str:
    """Format a column list + row lists into the compact `a | b` text block the LLM reads. Zero rows =
    no usable data (the query ran, matched nothing) -> the NO_DATA sentinel. Truncation is the CALLER's
    concern (it slices to its cap and appends its own 'omitted' note). Shared by every SQL adapter."""
    if not rows:
        return no_data()
    lines = [" | ".join(str(c) for c in cols)] + [" | ".join(str(v) for v in row) for row in rows]
    return "\n".join(lines)


def _rows_to_text(rows) -> str:
    """Snowpark rows (`.as_dict()`) -> text, via _table_to_text."""
    if not rows:
        return no_data()
    dicts = [r.as_dict() for r in rows]
    cols = list(dicts[0].keys())
    return _table_to_text(cols, [[d.get(c) for c in cols] for d in dicts])


def _enum_value(x) -> str:
    """The string of an SDK enum (or a plain string), upper-cased; '' for None."""
    return str(getattr(x, "value", x) or "").upper()


def _blank_strings_and_comments(sql: str) -> str:
    """Return sql with string literals, quoted identifiers, and comments removed, in ONE
    left-to-right pass that tracks lexical state. A single scanner -- unlike sequential regex
    passes -- can't be fooled by a marker of one form appearing inside another (NB1). Covers
    Snowflake's forms: `--` line, `/* */` block, `$$…$$` dollar string, `'…'` string (with both
    `''` and backslash `\\'` escapes), and `"…"` delimited identifier (with `""` escapes). This
    is only used to detect a real ';' statement separator; per the docstring on _ensure_read_only
    the SELECT-only role grant -- not this heuristic -- is the actual security boundary, so exotic
    forms it doesn't model (e.g. nested block comments) are an accepted limitation, not a hole."""
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
        elif sql[i] in ("'", '"'):                       # '…' string OR "…" delimited identifier
            q = sql[i]
            i += 1
            while i < n:
                c = sql[i]
                if c == "\\" and q == "'":               # backslash escape (single-quote strings only)
                    i += 2
                    continue
                if c == q:
                    if sql[i + 1:i + 2] == q:            # doubled quote -> escaped, stay inside
                        i += 2
                        continue
                    i += 1
                    break                                # closing quote
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
    def __init__(self, semantic: dict | None):
        # Resolve the semantic layer FIRST, so a missing/misconfigured layer fails with a clear
        # message BEFORE we open a Snowpark session (which needs creds + network and would
        # otherwise mask this error). Cortex Analyst accepts EITHER a native Semantic View (an
        # object already in the account) OR a semantic model YAML on a stage; prefer the view if
        # both are given. The binding is the pack's `snowflake:` block from
        # usecases/<pack>/semantic_layer.yaml, passed in by get_sql_tool -- engine/ NEVER reads env
        # for it (any env-vs-pack override is a TEST concern applied by the runner scripts).
        view = str((semantic or {}).get("view", "")).strip()         # DB.SCHEMA.MY_SEMANTIC_VIEW
        model = str((semantic or {}).get("model_file", "")).strip()  # @db.schema.stage/model.yaml
        if view:
            self._semantic = {"semantic_view": view}
        elif model:
            self._semantic = {"semantic_model_file": model}
        else:
            raise KeyError("Cortex Analyst pack declares no semantic layer: the `snowflake:` block "
                           "in the pack's semantic_layer.yaml needs `view:` (a native Semantic View) "
                           "or `model_file:` (a stage YAML).")
        self._view = view                                            # "" when using model_file (no TVF)
        self.last_sql = ""                                           # the SQL Analyst last generated (for auto value binding)
        self._dims_cache: list[str] | None = None
        from engine.llm_client import snowpark_session
        self._s = snowpark_session()                                 # runs the generated SQL

    def ask(self, question: str) -> str:
        import requests
        from engine.llm_client import snowflake_rest_base, snowflake_bearer_headers
        body = {"messages": [{"role": "user",
                              "content": [{"type": "text", "text": question}]}],
                **self._semantic}
        rest_timeout = max(5, int(os.getenv("CORTEX_ANALYST_TIMEOUT_SECONDS", "60")))  # tunable, like SQL exec
        url = f"{snowflake_rest_base()}/api/v2/cortex/analyst/message"
        for attempt in range(2):                     # one light retry on a TRANSIENT blip (Bug 5);
            try:                                     # 4xx/5xx raise HTTPError below and are NOT retried
                resp = requests.post(url, headers=snowflake_bearer_headers(),
                                     json=body, timeout=rest_timeout)
                resp.raise_for_status()
                break
            except (requests.ConnectionError, requests.Timeout):
                if attempt == 1:
                    raise
        content = resp.json().get("message", {}).get("content", [])
        # a sql-typed item WITHOUT a statement must not KeyError -- skip it (falls to the text branch)
        sql = next((c.get("statement") for c in content
                    if c.get("type") == "sql" and c.get("statement")), None)
        if not sql:                                                  # ambiguous Q / no SQL -> no data
            texts = [c.get("text", "") for c in content if c.get("type") == "text"]
            return no_data("\n".join(t for t in texts if t) or "Cortex Analyst returned no SQL.")
        sql = _ensure_read_only(sql)                                 # backstop: SELECT-only, single statement
        self.last_sql = sql                                          # so a blank can be diagnosed by its predicates
        max_rows = max(1, int(os.getenv("SQL_MAX_ROWS", "100")))     # how many result rows the LLM sees
        timeout = max(1, int(os.getenv("SQL_TIMEOUT_SECONDS", "30")))  # >=1: timeout=0 must not remove the bound
        rows = self._s.sql(sql).limit(max_rows + 1).collect(         # cap BEFORE collect() to bound memory
            statement_params={"STATEMENT_TIMEOUT_IN_SECONDS": str(timeout)})
        text = _rows_to_text(rows[:max_rows])
        return text + ("\n... (additional rows omitted)" if len(rows) > max_rows else "")

    def dimension_paths(self) -> list[str]:
        """OPTIONAL discovery capability: every dimension of the semantic view as a `TABLE.DIM` path, from
        `DESCRIBE SEMANTIC VIEW` (cached). Lets the engine resolve a bare column seen in a failed predicate
        to a dimension it can look up -- no hand-declared list needed. Parsed DEFENSIVELY (column names of
        DESCRIBE output vary by release): any failure, or a model_file pack, -> [] and the caller degrades
        to hand-declared dims / skills-only binding."""
        if self._dims_cache is not None:
            return self._dims_cache
        paths: list[str] = []
        if self._view:
            try:
                rows = self._s.sql(f"DESCRIBE SEMANTIC VIEW {self._view}").collect()
                for r in rows:
                    d = {str(k).lower(): v for k, v in r.as_dict().items()}
                    kind = str(d.get("object_kind") or d.get("kind") or d.get("object_type") or "").upper()
                    if "DIMENSION" not in kind:
                        continue
                    name = d.get("object_name") or d.get("name") or d.get("dimension")
                    parent = (d.get("parent_entity") or d.get("table") or d.get("logical_table")
                              or d.get("parent") or d.get("table_name"))
                    if name and parent:
                        paths.append(f"{parent}.{name}")
            except Exception:
                paths = []
        self._dims_cache = paths
        return paths

    def distinct_values(self, dim_paths, limit: int = 50) -> dict:
        """OPTIONAL discovery capability for skill-informed FRAMING: return the distinct values of each
        declared dimension so framing can bind a user's named value to its EXACT stored spelling (and
        never invent a literal like 'active'). Read-only, bounded, BEST-EFFORT -- a dim that errors, or a
        non-view semantic layer (model_file, so no SEMANTIC_VIEW() TVF), is silently skipped and the
        caller degrades to skills-only framing. `dim_paths` are pack-authored `TABLE.DIM` (optionally
        `DB.SCHEMA.TABLE.DIM`) identifiers -- author config, NEVER user input -- and are id-validated
        anyway before interpolation."""
        if not self._view or not dim_paths:                          # model_file packs can't use the TVF
            return {}
        limit = max(1, min(int(limit or 50), 1000))                  # bound memory regardless of config
        timeout = max(1, int(os.getenv("SQL_TIMEOUT_SECONDS", "30")))
        ident = re.compile(r"^[A-Za-z_][\w$]*(\.[A-Za-z_][\w$]*)+$")  # dotted id path only; else skip
        out = {}
        for path in dim_paths:
            p = str(path).strip()
            if not ident.match(p):                                   # not a clean TABLE.DIM path -> skip
                continue
            q = f"SELECT * FROM SEMANTIC_VIEW({self._view} DIMENSIONS {p}) LIMIT {limit}"
            try:
                rows = self._s.sql(q).collect(
                    statement_params={"STATEMENT_TIMEOUT_IN_SECONDS": str(timeout)})
            except Exception:                                        # best-effort: one bad dim never breaks framing
                continue
            seen, vals = set(), []
            for r in rows:
                d = r.as_dict()
                if not d:
                    continue
                v = next(iter(d.values()))                           # one dimension selected -> one column
                if v is None:
                    continue
                s = str(v).strip()
                if s and s not in seen:
                    seen.add(s)
                    vals.append(s)
            if vals:
                out[p] = vals
        return out


# Databricks Genie -- text-to-SQL over a Genie SPACE (Unity Catalog tables and/or a metric view), the
# Databricks mirror of CortexAnalystTool: one stateless question per call, the generated SQL is captured
# (`last_sql`) and passed through the read-only backstop, zero rows -> the NO_DATA sentinel, a Genie
# clarification (no SQL) -> the sentinel carrying the text as the reformulation hint.
#
# Pack config (usecases/<pack>/semantic_layer.yaml):
#     databricks:
#       genie_space: 01ef...            # REQUIRED for SQL_TOOL=genie -- Genie is addressed by space
#       metric_view: main.gold.sales    # OPTIONAL: the space's governed data source; with a warehouse
#                                       #   (DATABRICKS_WAREHOUSE_ID) it enables term->value binding
# Auth: the Databricks SDK's unified auth (DATABRICKS_HOST + DATABRICKS_TOKEN locally; injected creds
# inside model serving) via the memoized `databricks_workspace_client()`.
class GenieTool:
    def __init__(self, semantic: dict | None = None, client=None):   # semantic: the pack's `databricks:` block
        space = str((semantic or {}).get("genie_space", "")).strip()
        self._metric_view = str((semantic or {}).get("metric_view", "")).strip()
        if not space:                                                # config check FIRST (before any SDK)
            raise KeyError("Genie pack declares no Genie space: the `databricks:` block in the pack's "
                           "semantic_layer.yaml needs `genie_space: <space id>` (Genie is called by space; "
                           "`metric_view:` alone only names the data source).")
        self._space = space
        self.last_sql = ""                                           # the SQL Genie last generated (auto value binding)
        self._dims_cache: list[str] | None = None
        if client is None:
            from engine.llm_client import databricks_workspace_client
            client = databricks_workspace_client()
        self._w = client

    def ask(self, question: str) -> str:
        from datetime import timedelta
        timeout = max(5, int(os.getenv("GENIE_TIMEOUT_SECONDS", "120")))   # Genie plans + runs the SQL
        msg = self._w.genie.start_conversation_and_wait(self._space, question,
                                                        timeout=timedelta(seconds=timeout))
        status = _enum_value(getattr(msg, "status", None))
        if status and status != "COMPLETED":                         # backstop: the SDK waiter normally raises
            err = getattr(msg, "error", None)
            raise RuntimeError(f"Genie message ended with status {status}: "
                               f"{getattr(err, 'error', None) or getattr(err, 'message', None) or 'no detail'}")
        atts = list(getattr(msg, "attachments", None) or [])
        query_att = next((a for a in atts if getattr(getattr(a, "query", None), "query", None)), None)
        if query_att is None:                                        # ambiguous Q / clarification -> no data
            texts = [getattr(getattr(a, "text", None), "content", "") for a in atts]
            return no_data("\n".join(t for t in texts if t) or "Genie returned no SQL.")
        sql = _ensure_read_only(query_att.query.query)               # backstop: SELECT-only, single statement
        self.last_sql = sql                                          # so a blank can be diagnosed by its predicates
        res = self._w.genie.get_message_attachment_query_result(
            self._space, msg.conversation_id, msg.message_id, query_att.attachment_id)
        return self._statement_text(getattr(res, "statement_response", None), "Genie query")

    @staticmethod
    def _statement_text(stmt, what: str) -> str:
        """A Databricks StatementResponse (Genie's query result AND statement execution share the shape)
        -> the text block, capped at SQL_MAX_ROWS with a visible 'omitted' note."""
        state = _enum_value(getattr(getattr(stmt, "status", None), "state", None))
        if state and state != "SUCCEEDED":
            err = getattr(getattr(stmt, "status", None), "error", None)
            raise RuntimeError(f"{what} {state}: {getattr(err, 'message', None) or 'no detail'}")
        manifest = getattr(stmt, "manifest", None)
        cols = [c.name for c in (getattr(getattr(manifest, "schema", None), "columns", None) or [])]
        result = getattr(stmt, "result", None)
        rows = list(getattr(result, "data_array", None) or [])
        max_rows = max(1, int(os.getenv("SQL_MAX_ROWS", "100")))     # how many result rows the LLM sees
        more = (len(rows) > max_rows or bool(getattr(manifest, "truncated", False))
                or getattr(result, "next_chunk_index", None) is not None)
        text = _table_to_text(cols, rows[:max_rows])
        return text + ("\n... (additional rows omitted)" if more and not is_no_data(text) else "")

    # --- OPTIONAL discovery capabilities (need `metric_view` + DATABRICKS_WAREHOUSE_ID; else no-op) ------
    def _run_sql(self, sql: str, row_limit: int):
        """Read-only statement execution on the configured warehouse; (cols, rows). Bounded + timed."""
        wh = (os.getenv("DATABRICKS_WAREHOUSE_ID") or "").strip()
        if not wh:
            raise RuntimeError("DATABRICKS_WAREHOUSE_ID is not set")
        try:                                                         # the SDK enum; a plain string when the
            from databricks.sdk.service.sql import ExecuteStatementRequestOnWaitTimeout as _OWT   # SDK is absent
            cancel = _OWT.CANCEL                                     # (an injected test client)
        except ImportError:
            cancel = "CANCEL"
        wait = max(5, min(50, int(os.getenv("SQL_TIMEOUT_SECONDS", "30"))))   # the API allows 5..50s
        resp = self._w.statement_execution.execute_statement(
            statement=sql, warehouse_id=wh, wait_timeout=f"{wait}s", on_wait_timeout=cancel,
            row_limit=row_limit)
        state = _enum_value(getattr(getattr(resp, "status", None), "state", None))
        if state != "SUCCEEDED":
            raise RuntimeError(f"statement {state or 'unknown'}")
        cols = [c.name for c in (getattr(getattr(resp.manifest, "schema", None), "columns", None) or [])]
        return cols, list(getattr(resp.result, "data_array", None) or [])

    def dimension_paths(self) -> list[str]:
        """Every column of the declared metric view as `VIEW.COLUMN` (from DESCRIBE TABLE; cached).
        Measures are listed too -- a value lookup on one simply errors and is skipped. Any failure, or no
        metric_view / warehouse, -> [] (binding degrades to hand-declared dims / skills-only)."""
        if self._dims_cache is not None:
            return self._dims_cache
        paths: list[str] = []
        if self._metric_view:
            try:
                cols, rows = self._run_sql(f"DESCRIBE TABLE {self._metric_view}", 500)
                idx = cols.index("col_name") if "col_name" in cols else 0
                table = self._metric_view.split(".")[-1]
                for r in rows:
                    name = str((r[idx] if len(r) > idx else "") or "").strip()
                    if name and not name.startswith("#"):
                        paths.append(f"{table}.{name}")
            except Exception:
                paths = []
        self._dims_cache = paths
        return paths

    def distinct_values(self, dim_paths, limit: int = 50) -> dict:
        """Distinct values per `VIEW.COLUMN` path from the metric view (GROUP BY the dimension, which is how
        a metric view is read without MEASURE()). Read-only, bounded, best-effort; identifiers validated
        before interpolation (author config, never user input)."""
        if not self._metric_view or not dim_paths:
            return {}
        limit = max(1, min(int(limit or 50), 1000))
        ident = re.compile(r"^[A-Za-z_][\w$]*(\.[A-Za-z_][\w$]*)+$")
        out = {}
        for path in dim_paths:
            p = str(path).strip()
            if not ident.match(p):
                continue
            col = p.split(".")[-1]
            try:
                _, rows = self._run_sql(f"SELECT `{col}` FROM {self._metric_view} GROUP BY 1 LIMIT {limit}", limit)
            except Exception:                                        # a measure column / any error: skip
                continue
            seen, vals = set(), []
            for r in rows:
                v = r[0] if r else None
                if v is None:
                    continue
                s = str(v).strip()
                if s and s not in seen:
                    seen.add(s)
                    vals.append(s)
            if vals:
                out[p] = vals
        return out


def get_sql_tool(use_case: str | None = None, default: str = "mock",
                 semantic_layer: dict | None = None) -> SQLTool:
    # env SQL_TOOL wins; else the pack's default_sql_tool (passed in) -- no os.environ mutation,
    # so one graph's default can't leak to the next in the same process. (SQL_TOOL selects the tool
    # KIND; it is NOT functional config -- the semantic layer that follows comes from the PACK.)
    tool = (os.getenv("SQL_TOOL") or default).strip().lower()
    if tool == "mock":                                   # mock is a use-case fixture; semantic layer N/A
        mod = importlib.import_module(f"usecases.{use_case}.fixtures")
        return mod.MockSQLTool()
    if tool not in ("cortex", "genie"):
        raise ValueError(f"unknown SQL_TOOL: {tool!r} (expected mock|cortex|genie)")
    # A LIVE text-to-SQL tool needs a semantic layer DECLARED BY THE PACK. Select the block for the
    # active platform (cortex->snowflake, genie->databricks); a missing/empty one is a guard-rail
    # error -- an AI+BI pack MUST ship usecases/<pack>/semantic_layer.yaml (see load_semantic_layer).
    platform = "snowflake" if tool == "cortex" else "databricks"
    block = (semantic_layer or {}).get(platform)
    if not block:
        raise KeyError(
            f"SQL_TOOL={tool} needs a `{platform}:` semantic layer, but pack {use_case!r} declares "
            f"none. Add usecases/{use_case}/semantic_layer.yaml with a `{platform}:` block, or run "
            f"SQL_TOOL=mock (a non-AI+BI pack must not select a live text-to-SQL tool).")
    return CortexAnalystTool(block) if tool == "cortex" else GenieTool(block)
