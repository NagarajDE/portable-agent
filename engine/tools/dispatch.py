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

from engine.tools.base import (ToolContext, ToolError, ToolResult, Tool,
                               bound_output, redact, clean_error)

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


def _validation_summary(e: ValidationError) -> str:
    """A value-FREE summary of a pydantic validation error: field location + message + type only.
    NEVER echo the offending input (H4) -- pydantic's default str(e) embeds the input dict, which
    can carry a secret that a caller passed as a bad argument."""
    parts = []
    for err in e.errors(include_url=False):
        loc = ".".join(str(x) for x in err.get("loc", ())) or "(root)"
        parts.append(f"{loc}: {err.get('msg', 'invalid')} [{err.get('type', '')}]")
    return "; ".join(parts) or "input validation failed"


def _validate(spec, raw_input) -> dict:
    if not isinstance(raw_input, dict):
        raise ToolValidationError(f"tool input must be an object, got {type(raw_input).__name__}")
    if spec.input_model is None:
        return dict(raw_input)                       # passthrough: adapter accepts a free dict
    try:
        model = spec.input_model(**raw_input)
    except ValidationError as e:
        raise ToolValidationError(_validation_summary(e)) from None
    return model.model_dump()


def _emit(spec, status: str, ms: int, ctx, detail: str = "") -> None:
    """One structured, REDACTED log line per call. Fully defensive: a bad spec/ctx or a
    serialization error must never break the call (this runs in dispatch's finally)."""
    try:
        rec = {"event": "tool", "tool": getattr(spec, "name", "?"),
               "read_only": getattr(spec, "read_only", None),
               "status": status, "ms": ms, "run_id": getattr(ctx, "run_id", "-")}
        if detail:
            rec["detail"] = redact(detail)[:300]     # redact + bound; never a raw payload/secret
        _log.info(json.dumps(rec, default=str))
    except Exception:                                # observability must never break a call
        pass


def dispatch(tool: Tool, raw_input: dict, ctx: ToolContext) -> ToolResult:
    """Validate, gate, time-bound, run, normalize, log. Always returns a ToolResult, never raises."""
    t0 = time.perf_counter()
    status, detail = "ok", ""
    try:                                             # spec access is defensive (M4): a bad tool can't escape
        spec = tool.spec
    except Exception:
        spec = None
    try:
        if spec is None:
            raise ToolValidationError("tool has no spec")
        if not spec.read_only and ctx.approved is not True:      # STRICT: only literal True approves (M1)
            raise ApprovalRequiredError(
                f"tool {spec.name!r} is side-effecting; explicit approval required")
        validated = _validate(spec, raw_input)
        future = _EXECUTOR.submit(tool.run, validated, ctx)
        try:
            out = future.result(timeout=max(0.1, float(ctx.timeout_s)))
        except _FutureTimeout:
            future.cancel()                                      # drop it if still QUEUED (H1); a running
            raise ToolTimeoutError(                              # thread can't be force-killed in Python
                f"tool {spec.name!r} exceeded {ctx.timeout_s}s") from None
        # Normalize to a ToolResult and ENFORCE the result invariants (M7):
        if not isinstance(out, ToolResult):                      # tolerate a bare string/None
            out = ToolResult(ok=out is not None, output="" if out is None else str(out))
        ok = bool(out.ok) and out.error is None                  # ok=True WITH an error -> fail closed
        err = out.error
        if not ok and err is None:                               # ok=False WITHOUT an error -> synthesize
            err = ToolError("runtime", "tool returned ok=False without an error")
        if err is not None:
            err = ToolError(err.kind, clean_error(err.message))  # redact + bound tool-supplied message (H3)
        result = ToolResult(ok=ok, output=bound_output(out.output), error=err,
                            meta=out.meta if isinstance(out.meta, dict) else {})
        if not ok:
            status, detail = "error", f"{err.kind}: {err.message}"
        return result
    except ToolValidationError as e:
        status, detail = "invalid", str(e)
        return ToolResult(ok=False, error=ToolError("validation", clean_error(str(e))))
    except ApprovalRequiredError as e:
        status, detail = "blocked", str(e)
        return ToolResult(ok=False, error=ToolError("approval_required", clean_error(str(e))))
    except ToolTimeoutError as e:
        status, detail = "timeout", str(e)
        return ToolResult(ok=False, error=ToolError("timeout", clean_error(str(e))))
    except Exception as e:                                        # any tool bug -> normalized, never raised
        status, detail = "error", f"{type(e).__name__}: {e}"
        return ToolResult(ok=False, error=ToolError("runtime", clean_error(f"{type(e).__name__}: {e}")))
    finally:
        _emit(spec, status, round((time.perf_counter() - t0) * 1000), ctx, detail)
