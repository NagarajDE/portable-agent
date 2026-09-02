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


# Snowflake Cortex Analyst -- text-to-SQL over a semantic model.
class CortexAnalystTool:
    def __init__(self):
        from snowflake.snowpark import Session
        from engine.llm_client import sf_cfg
        self._s = Session.builder.configs(sf_cfg()).create()
        self._semantic_model = os.environ["CORTEX_SEMANTIC_MODEL"]   # @stage/model.yaml

    def ask(self, question: str) -> str:
        # POST /api/v2/cortex/analyst/message with the semantic model,
        # run the returned SQL, format rows.
        raise NotImplementedError("wire Cortex Analyst REST call here")


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
