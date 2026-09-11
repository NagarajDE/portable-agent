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


@dataclass(frozen=True)
class ToolSpec:
    """Tool identity + capability metadata. `read_only` is the SAFETY flag: only read-only
    tools are auto-run by the orchestrator; side-effecting tools require explicit approval
    (see dispatch). `input_model` (optional) is a pydantic model used to validate arguments
    BEFORE the tool runs -- unknown/malformed args fail clearly instead of reaching the tool."""
    name: str
    description: str
    read_only: bool = True
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
# Deliberately conservative: scrub bearer tokens, common secret key=value pairs, and JWTs.
_REDACT_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]+"),
    re.compile(r"(?i)\b(authorization|token|password|passwd|pat|secret|api[_-]?key)\b(\s*[=:]\s*)\S+"),
    re.compile(r"\beyJ[A-Za-z0-9._\-]{10,}"),          # JWT-ish
)


def redact(text: str) -> str:
    """Scrub obvious secrets from a string before it is logged or surfaced in an error."""
    if not text:
        return text
    out = str(text)
    out = _REDACT_PATTERNS[0].sub(r"\1[REDACTED]", out)
    out = _REDACT_PATTERNS[1].sub(r"\1\2[REDACTED]", out)
    out = _REDACT_PATTERNS[2].sub("[REDACTED_JWT]", out)
    return out
