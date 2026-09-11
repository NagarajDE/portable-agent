"""
ENGINE / tools / dispatch -- SAFE execution of a SINGLE tool call. This is the trust
boundary: everything a tool touches goes through here so the loop never has to.

Guarantees (per call):
  * INPUT VALIDATION  -- args validated against the tool's pydantic input_model (if any)
                         BEFORE the tool runs; malformed args fail as a normalized result.
  * APPROVAL GATE     -- a side-effecting tool (spec.read_only is False) runs ONLY when
                         ctx.approved is True; otherwise it's blocked (never executed).
  * TIMEOUT + BOUNDED -- runs on a bounded worker pool (TOOL_MAX_CONCURRENCY) with a
    CONCURRENCY         per-call timeout (ctx.timeout_s). A soft timeout: a hung call is
                         abandoned (Python can't force-kill a thread) -- network tools also
                         set their own transport timeout as the hard bound.
  * NORMALIZED ERRORS -- any exception becomes ToolResult(ok=False, error=ToolError(kind,..));
                         a failing tool never propagates an exception into the loop.
  * OUTPUT BOUNDED     -- result output is size-capped (bound_output) before returning.
  * OBSERVABILITY      -- exactly one structured, REDACTED log line per call
                         (tool, status, ms, run_id) -- never a credential or raw payload.

dispatch() is TOTAL: it always returns a ToolResult, never raises.
"""
from __future__ import annotations

import os
import json
import time
import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FutureTimeout

from pydantic import ValidationError

from engine.tools.base import ToolContext, ToolError, ToolResult, ToolSpec, Tool, bound_output, redact

_log = logging.getLogger("portable_agent.tools")


class ToolValidationError(ValueError):
    """Arguments failed validation against the tool's input_model (or were not a dict)."""


class ToolTimeoutError(TimeoutError):
    """The tool call exceeded ctx.timeout_s."""


class ApprovalRequiredError(PermissionError):
    """A side-effecting tool was dispatched without ctx.approved=True."""


# Bounded worker pool -> gives us BOTH a per-call timeout (future.result(timeout)) AND bounded
# concurrency for tool execution. Threads are created lazily on first submit.
_MAX_WORKERS = max(1, int(os.getenv("TOOL_MAX_CONCURRENCY", "4")))
_EXECUTOR = ThreadPoolExecutor(max_workers=_MAX_WORKERS, thread_name_prefix="tool")


def _validate(spec: ToolSpec, raw_input) -> dict:
    if not isinstance(raw_input, dict):
        raise ToolValidationError(f"tool input must be an object, got {type(raw_input).__name__}")
    if spec.input_model is None:
        return dict(raw_input)                       # passthrough: adapter accepts a free dict
    try:
        model = spec.input_model(**raw_input)
    except ValidationError as e:
        raise ToolValidationError(str(e)) from None
    return model.model_dump()


def _emit(spec: ToolSpec, status: str, ms: int, ctx: ToolContext, detail: str = "") -> None:
    rec = {"event": "tool", "tool": spec.name, "read_only": spec.read_only,
           "status": status, "ms": ms, "run_id": ctx.run_id}
    if detail:
        rec["detail"] = redact(detail)[:300]         # redact + bound; never a raw payload/secret
    try:
        _log.info(json.dumps(rec, default=str))
    except Exception:                                # observability must never break a call
        pass


def dispatch(tool: Tool, raw_input: dict, ctx: ToolContext) -> ToolResult:
    """Validate, gate, time-bound, run, normalize, log. Always returns a ToolResult."""
    spec = tool.spec
    t0 = time.perf_counter()
    status, detail, result = "ok", "", None
    try:
        if not spec.read_only and not ctx.approved:              # least-privilege / approval gate
            raise ApprovalRequiredError(
                f"tool {spec.name!r} is side-effecting; explicit approval required")
        validated = _validate(spec, raw_input)
        future = _EXECUTOR.submit(tool.run, validated, ctx)
        try:
            out = future.result(timeout=max(0.1, float(ctx.timeout_s)))
        except _FutureTimeout:
            raise ToolTimeoutError(f"tool {spec.name!r} exceeded {ctx.timeout_s}s") from None
        if not isinstance(out, ToolResult):                      # tolerate a bare string/None
            out = ToolResult(ok=out is not None, output="" if out is None else str(out))
        result = ToolResult(ok=out.ok, output=bound_output(out.output),
                            error=out.error, meta=out.meta)
        if not result.ok and result.error:
            status, detail = "error", f"{result.error.kind}: {result.error.message}"
        return result
    except ToolValidationError as e:
        status, detail = "invalid", str(e)
        return ToolResult(ok=False, error=ToolError("validation", str(e)))
    except ApprovalRequiredError as e:
        status, detail = "blocked", str(e)
        return ToolResult(ok=False, error=ToolError("approval_required", str(e)))
    except ToolTimeoutError as e:
        status, detail = "timeout", str(e)
        return ToolResult(ok=False, error=ToolError("timeout", str(e)))
    except Exception as e:                                        # any tool bug -> normalized, never raised
        status, detail = "error", f"{type(e).__name__}: {e}"
        return ToolResult(ok=False, error=ToolError("runtime", f"{type(e).__name__}: {e}"))
    finally:
        _emit(spec, status, round((time.perf_counter() - t0) * 1000), ctx, detail)
