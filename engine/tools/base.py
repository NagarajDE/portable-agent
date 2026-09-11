"""
ENGINE / tools / base -- the typed CONTRACTS every tool implements. No vendor SDKs,
no I/O, no registry logic; just the shapes + two safety helpers. Kept dependency-light
(only pydantic, already a repo dependency, for optional structured input validation).

A Tool is anything with a `spec` (identity + capability metadata) and a `run(input, ctx)`
that returns a normalized ToolResult. Errors are RETURNED as ToolError inside a ToolResult
(dispatch normalizes exceptions into that shape) so a failing tool never crashes the loop.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

# Default caps -- overridable per tool/adapter. Output is bounded so a runaway tool can't
# blow up prompt size / cost; treat all tool output as UNTRUSTED external data.
DEFAULT_MAX_OUTPUT_CHARS = 20_000
DEFAULT_MAX_ERROR_CHARS = 500          # returned error messages are bounded AND redacted (H3)


@dataclass(frozen=True)
class ToolSpec:
    """Tool identity + capability metadata. `read_only` is the SAFETY flag and is REQUIRED (no
    default) so an adapter author must consciously declare capability -- a write tool that forgot
    the flag can't silently be treated as safe (fail-closed at authoring). Only read-only tools
    are auto-run by the orchestrator; side-effecting tools require explicit approval (see dispatch).
    `input_model` (optional) is a pydantic model used to validate arguments BEFORE the tool runs
    -- unknown/malformed args fail clearly instead of reaching the tool."""
    name: str
    description: str
    read_only: bool
    input_model: type[BaseModel] | None = None


@dataclass(frozen=True)
class ToolError:
    """A normalized error carried inside a ToolResult. `kind` is a stable machine label
    (validation | approval_required | timeout | network | runtime | unknown_tool)."""
    kind: str
    message: str


@dataclass(frozen=True)
class ToolResult:
    """The normalized result of a tool run. `output` is text the agent may read as evidence;
    `error` is set (and ok=False) when the run failed. `meta` carries non-sensitive metadata."""
    ok: bool
    output: str = ""
    error: ToolError | None = None
    meta: dict = field(default_factory=dict)


@dataclass
class ToolContext:
    """Per-invocation execution context handed to every tool run. `run_id` is the loop's
    correlation id (joins tool logs to the run's trace events). `timeout_s` bounds the call.
    `approved` gates side-effecting tools -- it must be explicitly True for a write to run."""
    run_id: str = "-"
    timeout_s: float = 30.0
    approved: bool = False


@runtime_checkable
class Tool(Protocol):
    """The interface the loop depends on. Adapters set `spec` in __init__ and implement `run`."""
    spec: ToolSpec

    def run(self, input: dict, ctx: ToolContext) -> ToolResult: ...


def bound_output(text: str, max_chars: int = DEFAULT_MAX_OUTPUT_CHARS) -> str:
    """Cap tool output size so one tool can't dominate prompt tokens / cost. Appends a
    visible truncation note so the model knows the observation is partial."""
    if text is None:
        return ""
    text = str(text)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n... (output truncated at {max_chars} chars)"


# Redaction for LOG lines / error messages -- never let a credential land in observability.
# Ordered (pattern, replacement) pairs. Covers bearer tokens, sensitive key/value pairs in the
# common shapes -- env `k=v`, log `k: v`, AND JSON/dict `"k": "v"` / `'k': 'v'` including QUOTED
# values that contain spaces (`"password": "alpha beta"`) -- secrets embedded in URL userinfo
# (scheme://user:pass@host, as leaked by some SDK error strings), and JWTs. Best-effort defense in
# depth (a bare, unquoted secret containing spaces is only redacted up to the first space).
_SECRET_KEYS = r"authorization|token|password|passwd|pat|secret|api[_-]?key"
_REDACT_PATTERNS = (
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]+"), r"\1[REDACTED]"),
    # quoted value (may contain spaces): key: "alpha beta"  /  "key": 'v'
    (re.compile(rf"(?i)((?:{_SECRET_KEYS})[\"']?\s*[:=]\s*)([\"'])(?:(?!\2).)*\2"),
     r"\1\2[REDACTED]\2"),
    # unquoted value: key=hunter2  /  "key": abc
    (re.compile(rf"(?i)((?:{_SECRET_KEYS})[\"']?\s*[:=]\s*)[^\s,;\"'}}\])]+"), r"\1[REDACTED]"),
    # credentials in URL userinfo: scheme://user:pass@host
    (re.compile(r"(?i)([a-z][a-z0-9+.\-]*://)[^/@\s:]+:[^/@\s]+@"), r"\1[REDACTED]@"),
    (re.compile(r"\beyJ[A-Za-z0-9._\-]{10,}"), "[REDACTED_JWT]"),          # JWT-ish
)


def redact(text: str) -> str:
    """Scrub obvious secrets from a string before it is logged or surfaced in an error. Best-effort
    defense-in-depth (the primary control is never putting secrets in packs/args); applied to every
    dispatch log line AND every returned error message."""
    if not text:
        return text
    out = str(text)
    for pattern, repl in _REDACT_PATTERNS:
        out = pattern.sub(repl, out)
    return out


def clean_error(text: str, max_chars: int = DEFAULT_MAX_ERROR_CHARS) -> str:
    """Redact secrets AND bound size for an error string that is RETURNED to a caller (not only
    logged). Raw SDK/network exceptions can embed authenticated URLs, headers, or tokens and can be
    arbitrarily large -- both are unsafe to hand back verbatim."""
    red = redact("" if text is None else str(text))
    if len(red) > max_chars:
        return red[:max_chars] + "…(truncated)"
    return red
