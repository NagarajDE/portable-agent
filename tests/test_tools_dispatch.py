"""
Tests for the GENERIC tool machinery: registry, the dispatch trust boundary, and the
deterministic orchestrator. All in-process, no network, no credentials.

Covers the task's failure-mode matrix: unknown tools, malformed args, tool failures,
timeouts, oversized output, the approval gate for side-effecting tools, credential redaction
in logs, and an agent configured with NO tools.
"""
import logging

import pytest

from engine.tools import (
    build_tool, dispatch, known_tools, register, UnknownToolError,
    ToolContext, ToolResult, ToolError, ToolSpec, bound_output, redact,
)
from engine.tools.base import DEFAULT_MAX_OUTPUT_CHARS
from engine.tools.orchestrator import LoadedTool, load_tools, gather_context, describe_tools


def _ctx(timeout_s=2.0, approved=False):
    return ToolContext(run_id="test", timeout_s=timeout_s, approved=approved)


# --- registry --------------------------------------------------------------
def test_builtin_tools_registered():
    for name in ("mock", "sql", "http"):
        assert name in known_tools()


def test_build_unknown_tool_raises_with_known_list():
    with pytest.raises(UnknownToolError) as e:
        build_tool("does_not_exist", {})
    assert "does_not_exist" in str(e.value)


def test_register_rejects_empty_name():
    with pytest.raises(ValueError):
        register("   ", lambda p: None)


# --- dispatch: happy path + normalization ---------------------------------
def test_dispatch_ok_returns_toolresult():
    r = dispatch(build_tool("mock", {"output": "hello"}), {"query": "x"}, _ctx())
    assert isinstance(r, ToolResult) and r.ok and r.output == "hello"


def test_dispatch_malformed_args_are_validation_error():
    # the sql bridge requires a `question`; an empty dict must fail validation, not reach the tool
    r = dispatch(build_tool("sql", {"default_sql_tool": "mock"}), {}, _ctx())
    assert not r.ok and r.error.kind == "validation"


def test_dispatch_non_dict_input_is_validation_error():
    r = dispatch(build_tool("mock", {"output": "x"}), "not-a-dict", _ctx())
    assert not r.ok and r.error.kind == "validation"


def test_dispatch_tool_failure_is_normalized_not_raised():
    r = dispatch(build_tool("mock", {"fail": "boom"}), {}, _ctx())
    assert not r.ok and r.error.kind == "runtime" and "boom" in r.error.message


def test_dispatch_timeout_is_normalized():
    r = dispatch(build_tool("mock", {"delay_s": 0.6, "output": "late"}), {}, _ctx(timeout_s=0.1))
    assert not r.ok and r.error.kind == "timeout"


def test_dispatch_output_is_bounded():
    big = "x" * (DEFAULT_MAX_OUTPUT_CHARS + 5_000)
    r = dispatch(build_tool("mock", {"output": big}), {}, _ctx())
    assert r.ok and len(r.output) <= DEFAULT_MAX_OUTPUT_CHARS + 80
    assert "truncated" in r.output


# --- dispatch: least-privilege / approval gate ----------------------------
def test_side_effecting_tool_blocked_without_approval():
    r = dispatch(build_tool("mock", {"read_only": False, "output": "wrote"}), {}, _ctx(approved=False))
    assert not r.ok and r.error.kind == "approval_required"


def test_side_effecting_tool_runs_with_approval():
    r = dispatch(build_tool("mock", {"read_only": False, "output": "wrote"}), {}, _ctx(approved=True))
    assert r.ok and r.output == "wrote"


def test_read_only_tool_runs_without_approval():
    r = dispatch(build_tool("mock", {"output": "read"}), {}, _ctx(approved=False))
    assert r.ok


# --- observability: one redacted log line, no secret leakage --------------
def test_dispatch_logs_are_redacted():
    logger = logging.getLogger("portable_agent.tools")
    captured = []

    class _Capture(logging.Handler):
        def emit(self, record):
            captured.append(record.getMessage())

    handler = _Capture()
    logger.addHandler(handler)
    prev_level = logger.level
    logger.setLevel(logging.INFO)
    try:
        dispatch(build_tool("mock", {"fail": "token=SUPERSECRET123"}), {}, _ctx())
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prev_level)
    line = "\n".join(captured)
    assert "SUPERSECRET123" not in line and "[REDACTED]" in line
    assert '"event": "tool"' in line and '"status": "error"' in line


# --- helpers ---------------------------------------------------------------
def test_bound_output_caps_and_notes():
    assert bound_output("x" * 30, 10).startswith("x" * 10)
    assert "truncated" in bound_output("x" * 30, 10)
    assert bound_output("short", 100) == "short"


def test_redact_scrubs_common_secret_shapes():
    assert "abc.def" not in redact("Authorization: Bearer abc.def")
    assert "hunter2" not in redact("password=hunter2")
    assert "REDACTED" in redact("api_key: 12345")


# --- orchestrator: deterministic, read-only auto-run ----------------------
def test_load_tools_none_only_for_absent_key():
    # ONLY an absent `tools:` key -> None (the legacy single-SQL path)
    assert load_tools("some_uc", {"default_sql_tool": "mock"}) is None


def test_load_tools_null_tools_raises():
    with pytest.raises(ValueError):
        load_tools("uc", {"tools": None})               # explicit null must NOT silently mean legacy (M6)


def test_load_tools_empty_list_is_toolless():
    # explicit `tools: []` is a deliberately toolless agent (distinct from the legacy None)
    assert load_tools("uc", {"tools": []}) == []


def test_load_tools_malformed_value_raises():
    for bad in ({}, False, "sql", 3):                           # present but not a list -> error, not silently empty
        with pytest.raises(ValueError):
            load_tools("uc", {"tools": bad})


def test_gather_context_with_no_tools_is_safe():
    assert gather_context("q", [], "rid") == "No tool observations."


def test_load_tools_builds_and_gather_runs_multiple(monkeypatch):
    cfg = {"tools": [
        {"type": "mock", "name": "catalog", "input": {"query": "${task}"}, "params": {"output": "CAT"}},
        {"type": "mock", "name": "tickets", "input": {"q": "${task}"}, "params": {"output": "TIX"}},
    ]}
    loaded = load_tools("uc", cfg)
    assert [lt.label for lt in loaded] == ["catalog", "tickets"]
    ctxt = gather_context("hello", loaded, "rid")
    assert "[catalog]" in ctxt and "CAT" in ctxt
    assert "[tickets]" in ctxt and "TIX" in ctxt


def test_gather_context_substitutes_task_into_input():
    cfg = {"tools": [{"type": "mock", "name": "echo", "input": {"query": "${task}"}}]}
    loaded = load_tools("uc", cfg)
    out = gather_context("FIND-ME", loaded, "rid")
    assert "FIND-ME" in out                       # ${task} was rendered into the tool input (echoed)


def test_gather_context_skips_side_effecting_tools():
    cfg = {"tools": [
        {"type": "mock", "name": "reader", "params": {"output": "R"}},
        {"type": "mock", "name": "writer", "params": {"read_only": False, "output": "W"}},
    ]}
    loaded = load_tools("uc", cfg)
    out = gather_context("q", loaded, "rid")
    assert "[reader]" in out and "R" in out
    assert "[writer]" not in out and "W" not in out   # side-effecting tool NOT auto-run


def test_gather_context_degrades_on_tool_failure():
    cfg = {"tools": [{"type": "mock", "name": "flaky", "params": {"fail": "down"}}]}
    loaded = load_tools("uc", cfg)
    out = gather_context("q", loaded, "rid")
    assert "[flaky]" in out and "unavailable" in out   # failure noted, run not broken


def test_load_tools_bad_entry_raises():
    with pytest.raises(UnknownToolError):
        load_tools("uc", {"tools": [{"type": "nope"}]})
    with pytest.raises(ValueError):
        load_tools("uc", {"tools": [{"name": "missing-type"}]})


def test_describe_tools_lists_capability():
    cfg = {"tools": [
        {"type": "mock", "name": "reader", "params": {"description": "reads"}},
        {"type": "mock", "name": "writer", "params": {"read_only": False, "description": "writes"}},
    ]}
    desc = describe_tools(load_tools("uc", cfg))
    assert "reader (read-only)" in desc
    assert "writer (SIDE-EFFECTING (requires approval))" in desc
    assert describe_tools([]) == "None."


# --- hardening: strict approval, extra-arg rejection, result invariants, redaction --------
def test_approval_gate_requires_literal_true():
    tool = build_tool("mock", {"read_only": False, "output": "wrote"})
    for approved in ("true", 1, "yes", [1]):          # truthy but NOT the literal True -> still blocked (M1)
        r = dispatch(tool, {}, ToolContext(run_id="t", timeout_s=1.0, approved=approved))
        assert not r.ok and r.error.kind == "approval_required"
    assert dispatch(tool, {}, ToolContext(run_id="t", timeout_s=1.0, approved=True)).ok


def test_dispatch_rejects_unknown_args():
    # the sql bridge input_model forbids extras -> an unknown arg is a validation error (M3)
    tool = build_tool("sql", {"default_sql_tool": "mock", "use_case": "dq_qals"})
    r = dispatch(tool, {"question": "q", "bogus": "x"}, _ctx())
    assert not r.ok and r.error.kind == "validation"


class _FixedResult:
    """A tool that returns a caller-supplied (possibly malformed) ToolResult, to test invariants."""
    def __init__(self, res):
        self.spec = ToolSpec("fixed", "d", read_only=True, input_model=None)
        self._res = res

    def run(self, input, ctx):
        return self._res


def test_dispatch_normalizes_ok_false_without_error():
    r = dispatch(_FixedResult(ToolResult(ok=False, output="x")), {}, _ctx())   # ok=False, error=None (M7)
    assert not r.ok and r.error is not None and r.error.kind == "runtime"


def test_dispatch_fails_closed_on_ok_true_with_error():
    bad = ToolResult(ok=True, output="x", error=ToolError("runtime", "boom"))  # contradictory (M7)
    r = dispatch(_FixedResult(bad), {}, _ctx())
    assert not r.ok                                    # presence of an error is authoritative -> failure


def test_returned_error_is_redacted_and_bounded():
    # H3: a tool exception carrying a secret must not leak in the RETURNED error message
    r = dispatch(build_tool("mock", {"fail": "boom token=SECRETXYZ"}), {}, _ctx())
    assert not r.ok and "SECRETXYZ" not in r.error.message and "[REDACTED]" in r.error.message


def test_validation_error_has_no_input_values():
    # H4: a rejected extra arg must surface the field NAME, never the (possibly secret) value
    tool = build_tool("sql", {"default_sql_tool": "mock", "use_case": "dq_qals"})
    r = dispatch(tool, {"question": "q", "password": "HUNTER2SECRET"}, _ctx())
    assert not r.ok and r.error.kind == "validation"
    assert "HUNTER2SECRET" not in r.error.message and "password" in r.error.message


def test_redact_scrubs_json_dict_and_url_forms():
    assert "hunter2" not in redact('{"password": "hunter2"}')       # JSON
    assert "hunter2" not in redact("{'password': 'hunter2'}")       # python dict repr
    assert "s3cr3t" not in redact("token=s3cr3t&x=1")               # querystring / env
    assert "secretpw" not in redact("https://user:secretpw@host/x") # URL userinfo


def test_redact_scrubs_quoted_secret_with_spaces():
    assert "beta" not in redact('{"password": "alpha beta"}')       # H4: quoted value with a space


class _CountingWrite:
    """A side-effecting tool that records whether run() ever executed."""
    def __init__(self):
        self.spec = ToolSpec("writer", "d", read_only=False, input_model=None)
        self.ran = 0

    def run(self, input, ctx):
        self.ran += 1
        return ToolResult(ok=True, output="did")


def test_blocked_side_effecting_tool_never_executes():
    tool = _CountingWrite()
    r = dispatch(tool, {}, _ctx(approved=False))
    assert not r.ok and r.error.kind == "approval_required"
    assert tool.ran == 0                               # proves it was NOT executed, only blocked


def test_returned_error_is_size_bounded():
    from engine.tools.base import DEFAULT_MAX_ERROR_CHARS
    r = dispatch(build_tool("mock", {"fail": "x" * 5000}), {}, _ctx())   # huge exception message
    assert not r.ok and len(r.error.message) <= DEFAULT_MAX_ERROR_CHARS + 20


def test_accepts_timeout_only_when_keyword_passable():
    # NB1: timeout_s is forwarded as a KEYWORD (ask(question, timeout_s=...)), so a positional-only
    # timeout_s must NOT count as supported -- else the forward would raise TypeError.
    from engine.tools.sql_bridge import SqlBridgeTool
    A = SqlBridgeTool._accepts_timeout

    def ask_kw(self, q, timeout_s=1.0): ...
    def ask_kwonly(self, q, *, timeout_s=1.0): ...
    def ask_kwargs(self, q, **kw): ...
    def ask_plain(self, q): ...
    def ask_posonly(self, q, timeout_s=1.0, /): ...

    assert A(ask_kw) is True and A(ask_kwonly) is True and A(ask_kwargs) is True
    assert A(ask_plain) is False
    assert A(ask_posonly) is False       # positional-only can't be passed by keyword
