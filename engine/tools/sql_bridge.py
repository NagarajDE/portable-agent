"""
ENGINE / tools / sql_bridge -- the COMPATIBILITY layer that turns the existing SQLTool into
a generic Tool, so SQL becomes ONE tool category rather than the engine's assumption.

This is how a NEW multi-tool pack can include SQL alongside other tools. EXISTING SQL packs
do NOT go through here -- they keep the identical legacy `get_sql_tool().ask()` path in
engine/graph.py (zero behavior change). The Snowflake/Databricks SDKs stay lazy (imported
only when the wrapped SQL tool is first built), so the mock path never needs them.
"""
from __future__ import annotations

from pydantic import BaseModel

from engine.tools.base import ToolContext, ToolResult, ToolSpec
from engine.tools.registry import register


class _SqlInput(BaseModel):
    question: str


class SqlBridgeTool:
    """Adapts engine.sql_tool.get_sql_tool(...) (mock | cortex | genie) to the Tool interface.
    read_only=True: text-to-SQL retrieval never writes (the SQL tool itself also enforces a
    SELECT-only heuristic + the deploy grant is the real boundary)."""

    def __init__(self, params: dict):
        self.spec = ToolSpec(
            name=params.get("tool_name", "sql"),
            description="Text-to-SQL data retrieval over the configured semantic layer.",
            read_only=True,
            input_model=_SqlInput,
        )
        self._use_case = params.get("use_case")
        self._default = params.get("default_sql_tool", "mock")
        self._inner = None                           # built lazily -> vendor SDK import is lazy

    def _tool(self):
        if self._inner is None:
            from engine.sql_tool import get_sql_tool
            self._inner = get_sql_tool(self._use_case, self._default)
        return self._inner

    def run(self, input: dict, ctx: ToolContext) -> ToolResult:
        text = self._tool().ask(input["question"])
        return ToolResult(ok=True, output=text if text is not None else "")


register("sql", SqlBridgeTool)
