"""
ENGINE / tools -- a SELF-CONTAINED, modular package for the generic tool layer.

This package generalizes the agent from SQL-only to ANY tool-using agent WITHOUT
touching the generate->evaluate->refine loop, the scoring system, LLMClient, or any
existing SQL pack. SQL becomes ONE tool category (see sql_bridge) rather than the
engine's universal assumption.

Layout (one concern per file, same seam pattern as llm_client / sql_tool / tracing):
  base.py          typed contracts: ToolSpec / ToolResult / ToolError / ToolContext / Tool
  registry.py      name -> factory registration + build_tool + unknown-tool error
  dispatch.py      safe single-tool execution: validate -> timeout -> normalize -> log
  orchestrator.py  load_tools(cfg) + gather_context(task) over READ-ONLY tools (deterministic)
  sql_bridge.py    wraps engine.sql_tool.get_sql_tool as a Tool (SQL = one tool category)
  mock_tool.py     deterministic in-process tool (creds-free tests + demos)
  http_tool.py     generic HTTP GET reference with allowlist/redirect/SSRF/redaction/bounds

Public contracts are re-exported here so callers can `from engine.tools import ...`.
"""
from __future__ import annotations

from engine.tools.base import (
    ToolSpec, ToolResult, ToolError, ToolContext, Tool, bound_output, redact,
)
from engine.tools.registry import register, build_tool, known_tools, UnknownToolError
from engine.tools.dispatch import (
    dispatch, ToolValidationError, ToolTimeoutError, ApprovalRequiredError,
)
from engine.tools.orchestrator import LoadedTool, load_tools, gather_context, describe_tools
from engine.tools.agentic import run_agentic       # opt-in model-driven gathering (tool_mode: agentic)

# Import the builtin adapters for their registration side-effect, so `known_tools()` is
# populated on `import engine.tools`. Their heavy/vendor deps stay lazy inside run().
from engine.tools import sql_bridge, mock_tool, http_tool, mcp_tool  # noqa: F401,E402

__all__ = [
    "ToolSpec", "ToolResult", "ToolError", "ToolContext", "Tool", "bound_output", "redact",
    "register", "build_tool", "known_tools", "UnknownToolError",
    "dispatch", "ToolValidationError", "ToolTimeoutError", "ApprovalRequiredError",
    "LoadedTool", "load_tools", "gather_context", "describe_tools", "run_agentic",
]
