"""
ENGINE / settings -- the ONE place the loop reads DEPLOYMENT configuration from the process
environment. (Vendor adapters still read their own credentials/SDK env -- that is deployment config the
platform injects, e.g. SPCS OAuth, and belongs at the adapter edge. Nothing in engine/ ever reads the
`.env` FILE; only the runner/test scripts load it.)

Read once per graph build via `settings()`; pass the object down rather than calling os.getenv in nodes.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    sql_tool: str | None            # SQL_TOOL override (mock|cortex|genie); None -> pack default
    tool_timeout_s: float           # per-tool-call wall clock (dispatch)
    trace_content: bool             # include answer/verdict text in trace events


def settings() -> Settings:
    tmo = os.getenv("TOOL_TIMEOUT_SECONDS", "30")
    try:
        timeout = max(1.0, float(tmo))
    except ValueError:
        raise ValueError("TOOL_TIMEOUT_SECONDS must be a number")
    return Settings(
        sql_tool=(os.getenv("SQL_TOOL") or "").strip().lower() or None,
        tool_timeout_s=timeout,
        trace_content=os.getenv("TRACE_INCLUDE_CONTENT", "false").strip().lower() in ("1", "true", "yes"),
    )
