"""
ENGINE / tools / sql_bridge -- the COMPATIBILITY layer that turns the existing SQLTool into
a generic Tool, so SQL becomes ONE tool category rather than the engine's assumption.

This is how a NEW multi-tool pack can include SQL alongside other tools. EXISTING SQL packs
do NOT go through here -- they keep the identical legacy `get_sql_tool().ask()` path in
engine/graph.py (zero behavior change). The Snowflake/Databricks SDKs stay lazy (imported
only when the wrapped SQL tool is first built), so the mock path never needs them.
"""
from __future__ import annotations

import inspect
import threading

from pydantic import BaseModel, ConfigDict

from engine.tools.base import ToolContext, ToolResult, ToolSpec
from engine.tools.registry import register


class _SqlInput(BaseModel):
    model_config = ConfigDict(extra="forbid")        # reject unknown args instead of dropping them (M3)
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
        self._lock = threading.Lock()                # guard lazy init against concurrent callers (M8)

    def _tool(self):
        if self._inner is None:                      # double-checked: cheap read, then lock once
            with self._lock:
                if self._inner is None:
                    from engine.sql_tool import get_sql_tool
                    from engine.graph import load_semantic_layer   # lazy: avoid graph<->tools import cycle
                    # A `sql` tool inside a tools-pack self-declares its layer the same way the legacy
                    # path does -- read from the PACK (usecases/<pack>/semantic_layer.yaml), never env.
                    semantic = load_semantic_layer(self._use_case)
                    self._inner = get_sql_tool(self._use_case, self._default, semantic)
        return self._inner

    @staticmethod
    def _accepts_timeout(ask) -> bool:
        """True if the adapter's ask() can receive `timeout_s` BY KEYWORD (a normal/keyword-only
        param) or via **kwargs. A POSITIONAL-ONLY `timeout_s` is deliberately NOT counted (NB1):
        we call ask(question, timeout_s=...), so claiming support for a positional-only param would
        raise TypeError. Lets us forward ctx.timeout_s to adapters that support it WITHOUT changing
        the SQLTool.ask(question) contract or breaking adapters that don't (H2) -- the transport
        timeout is then enforced adapter-side, which caller-side future.cancel() cannot do."""
        try:
            params = inspect.signature(ask).parameters.values()
        except (TypeError, ValueError):
            return False
        for p in params:
            if p.kind == p.VAR_KEYWORD:                                    # **kwargs -> accepts it
                return True
            if p.name == "timeout_s" and p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY):
                return True
        return False

    def run(self, input: dict, ctx: ToolContext) -> ToolResult:
        tool = self._tool()
        kwargs = {"timeout_s": ctx.timeout_s} if self._accepts_timeout(tool.ask) else {}
        text = tool.ask(input["question"], **kwargs)
        return ToolResult(ok=True, output=text if text is not None else "")


register("sql", SqlBridgeTool)
