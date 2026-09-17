"""The opt-in agentic (model-driven) tool-gathering loop + the MCP adapter's construction contract.
All offline/deterministic: a scripted worker emits JSON tool-calls; mock tools provide observations."""
import pytest

import engine.graph as G
from engine.graph import build_graph, initial_state
from engine.tools import LoadedTool, build_tool, run_agentic, UnknownToolError
from engine.tools.mock_tool import MockTool

_ACT = "Q:{task}\nAVAILABLE TOOLS:\n{tools}\nEVIDENCE:\n{observations}"


def _loaded(**params):
    params.setdefault("tool_name", params.get("name", "catalog"))
    t = MockTool(params)
    return LoadedTool(label=t.spec.name, tool=t, input_template={})


class _Script:
    """A worker that returns scripted JSON tool-call strings in order (repeats the last)."""
    def __init__(self, *replies):
        self.replies, self.i = list(replies), 0

    def complete(self, prompt, **k):
        v = self.replies[min(self.i, len(self.replies) - 1)]
        self.i += 1
        return v


# --- run_agentic (unit) ----------------------------------------------------
def test_agentic_gathers_a_tool_then_finals():
    loaded = [_loaded(tool_name="catalog", output="CATALOG_ROWS")]
    w = _Script('{"tool": "catalog", "input": {"query": "x"}}', '{"final": true}')
    obs = run_agentic("q", loaded, w, _ACT, max_steps=4)
    assert "CATALOG_ROWS" in obs                              # the read-only tool ran, evidence gathered


def test_agentic_skips_side_effecting_tool_without_running_it():
    # M13: prove NON-EXECUTION with a spy -- the write tool's run() must never be called.
    from engine.tools.base import ToolSpec, ToolResult

    class _SpyWriter:
        def __init__(self):
            self.spec = ToolSpec(name="writer", description="side-effecting", read_only=False)
            self.ran = False

        def run(self, input, ctx):
            self.ran = True
            return ToolResult(ok=True, output="SHOULD_NOT_RUN")

    spy = _SpyWriter()
    loaded = [LoadedTool(label="writer", tool=spy, input_template={})]
    w = _Script('{"tool": "writer", "input": {}}')            # always tries the write tool
    obs = run_agentic("q", loaded, w, _ACT, max_steps=2)
    assert spy.ran is False                                   # never auto-run a write (execution spy)
    assert "SHOULD_NOT_RUN" not in obs and "approval" in obs.lower()


def test_agentic_unknown_tool_is_noted_not_fatal():
    loaded = [_loaded(tool_name="catalog", output="ROWS")]
    w = _Script('{"tool": "ghost", "input": {}}', '{"final": true}')
    obs = run_agentic("q", loaded, w, _ACT, max_steps=3)
    assert "no such tool" in obs.lower()


def test_agentic_dedupes_identical_calls():
    loaded = [_loaded(tool_name="catalog", output="ROWS")]
    w = _Script('{"tool": "catalog", "input": {"q": 1}}')     # same call every step
    obs = run_agentic("q", loaded, w, _ACT, max_steps=4)
    assert obs.count("ROWS") == 1 and "already called" in obs.lower()


def test_agentic_worker_error_propagates_not_masked(monkeypatch):
    # M3: a config/auth error during gathering must SURFACE (gathering is part of generate, which is
    # fatal-on-failure), not be swallowed as "no tool needed".
    class _Boom:
        def complete(self, prompt, **k):
            raise RuntimeError("bad LLM config")
    with pytest.raises(RuntimeError):
        run_agentic("q", [_loaded(output="X")], _Boom(), _ACT, max_steps=3)


def test_agentic_single_pass_render_no_resubstitution():
    # H1: a task containing a literal "{tools}" must NOT be re-substituted by the tools block.
    seen = {}
    class _W:
        def complete(self, prompt, **k):
            seen["prompt"] = prompt
            return '{"final": true}'
    run_agentic("what about {tools} here", [_loaded(output="X")], _W(),
                "Q:{task}\nTOOLS:{tools}\nOBS:{observations}", max_steps=1)
    # the literal {tools} from the task survives verbatim (single-pass fill), appearing twice:
    assert seen["prompt"].count("{tools}") == 1                # only the task's literal remains
    assert "Q:what about {tools} here" in seen["prompt"]


# --- end-to-end through build_graph (agentic pack) -------------------------
class _MarkerWorker:
    """Answers the act-selection step (marker in prompt) with JSON, and the generate step with text.
    Records the generate prompt so we can assert gathered evidence reached it."""
    def __init__(self):
        self.act = 0
        self.gen_prompt = None

    def complete(self, prompt, **k):
        if "AVAILABLE TOOLS" in prompt:
            self.act += 1
            return '{"tool": "catalog", "input": {"query": "x"}}' if self.act == 1 else '{"final": true}'
        self.gen_prompt = prompt
        return "final answer from evidence"


def test_agentic_end_to_end_feeds_evidence_into_generate(monkeypatch):
    cfg = {"max_score": 18, "pass_score": 18, "max_iters": 0, "eval_retries": 0, "max_stall": 0,
           "default_sql_tool": "mock", "loop": True, "tool_mode": "agentic", "max_tool_steps": 3,
           "tools": [{"type": "mock", "name": "catalog",
                      "input": {"query": "${task}"}, "params": {"output": "CATALOG_ROWS"}}]}
    monkeypatch.setattr(G, "load_config", lambda uc: dict(cfg))
    w = _MarkerWorker()
    g = build_graph("dq_qals", llm=w, eval_llm=_Script("SCORE: 18/18 - ok"), verbose=False)
    f = g.invoke(initial_state("q"))
    assert f["best_answer"] == "final answer from evidence"
    assert w.act == 2                                         # gathered once, then finalized
    assert "CATALOG_ROWS" in (w.gen_prompt or "")            # evidence reached the generate step


def test_incident_triage_pack_gathers_live_tools():
    # DEMONSTRATION: the real agentic example pack, driven by a scripted JSON worker, actually calls
    # its read-only tools (mock catalog + http-mock health) and their observations reach `generate` --
    # and the pack's operator instructions are injected too.
    class W:
        def __init__(self):
            self.act, self.gen = 0, None

        def complete(self, prompt, **k):
            if "AVAILABLE TOOLS" in prompt:              # act-selection step (act.md marker)
                self.act += 1
                if self.act == 1:
                    return '{"tool": "catalog", "input": {"query": "orders-api"}}'
                if self.act == 2:
                    return '{"tool": "health", "input": {"path": "/health/orders-api"}}'
                return '{"final": true}'
            self.gen = prompt                            # the generate step
            return "orders-api is degraded; owner team-fulfillment"

    w = W()
    g = build_graph("incident_triage", llm=w, eval_llm=_Script("SCORE: 18/18 - ok"), verbose=False)
    f = g.invoke(initial_state("Why is orders-api failing and who owns it?"))
    assert w.act >= 3                                    # gathered catalog + health, then finalized
    assert "team-fulfillment" in w.gen                  # catalog observation reached generate
    assert "degraded" in w.gen                          # http-mock health observation reached generate
    assert "OPERATOR INSTRUCTIONS" in w.gen             # the pack's instructions/ block is injected
    assert f["best_answer"].startswith("orders-api is degraded")


def test_agentic_without_tools_is_rejected(monkeypatch):
    monkeypatch.setattr(G, "load_config", lambda uc: {"tool_mode": "agentic", "max_iters": 1})
    with pytest.raises(ValueError):                           # agentic needs a tools: list
        build_graph("dq_qals", llm=_Script("x"), eval_llm=_Script("SCORE: 1/18 - y"), verbose=False)


# --- MCP adapter construction contract (live call untested; no server) -----
def test_mcp_tool_builds_fail_closed_read_only_default():
    t = build_tool("mcp", {"tool": "get_issue", "command": "run-server"})
    assert t.spec.read_only is False                         # fail-closed: unknown capability
    t2 = build_tool("mcp", {"tool": "get_issue", "command": "run-server", "read_only": True})
    assert t2.spec.read_only is True


def test_mcp_tool_requires_tool_and_transport():
    with pytest.raises(ValueError):
        build_tool("mcp", {"command": "run-server"})         # missing `tool`
    with pytest.raises(ValueError):
        build_tool("mcp", {"tool": "get_issue"})             # missing command (url/SSE not wired -> M5)


# --- H4: strict boolean parsing (security-sensitive flags must not fail open) ---
def test_strict_bool_rejects_and_parses():
    from engine.tools import strict_bool
    assert strict_bool(True, False) is True and strict_bool(False, True) is False
    assert strict_bool(None, True) is True                    # None -> default
    assert strict_bool("false", True) is False               # the quoted-YAML fail-open case
    assert strict_bool("TRUE", False) is True
    with pytest.raises(ValueError):
        strict_bool("maybe", False)                          # a typo is surfaced, not guessed


def test_mock_tool_read_only_string_false_is_not_fail_open():
    # H4: read_only: "false" (a truthy string) must be parsed to False, i.e. side-effecting.
    t = build_tool("mock", {"tool_name": "w", "read_only": "false"})
    assert t.spec.read_only is False
