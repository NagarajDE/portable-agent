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


def test_agentic_skips_side_effecting_tool():
    loaded = [_loaded(tool_name="writer", read_only=False, output="SHOULD_NOT_RUN")]
    w = _Script('{"tool": "writer", "input": {}}')            # always tries the write tool
    obs = run_agentic("q", loaded, w, _ACT, max_steps=2)
    assert "SHOULD_NOT_RUN" not in obs and "approval" in obs.lower()   # never auto-run a write


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


def test_agentic_worker_error_is_non_fatal():
    class _Boom:
        def complete(self, prompt, **k):
            raise RuntimeError("worker down")
    obs = run_agentic("q", [_loaded(output="X")], _Boom(), _ACT, max_steps=3)
    assert obs == "No tool observations."                     # degrades, never raises


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
           "default_sql_tool": "mock", "tool_mode": "agentic", "max_tool_steps": 3,
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
        build_tool("mcp", {"tool": "get_issue"})             # missing command/url
