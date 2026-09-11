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
    ToolContext, ToolResult, ToolSpec, bound_output, redact,
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
def test_load_tools_empty_for_legacy_pack():
    # a pack with no `tools:` key -> [] (signals the legacy single-SQL path in graph)
    assert load_tools("some_uc", {"default_sql_tool": "mock"}) == []


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
