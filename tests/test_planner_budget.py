"""R4 -- planned-mode latency/robustness knobs: the planner's OWN output-token budget (PLAN_MAX_TOKENS, never
below LLM_MAX_TOKENS) passed per call, an ACTIONABLE truncation diagnostic, and the optional separate
PLANNER model (models.planner / PLANNER_*), default = the worker. Offline."""
import json

import pytest

import engine.graph as G
from engine.graph import build_graph, initial_state
from engine.llm_client import _resolve_planner, get_planner_client, model_summary, _max_tokens
from engine.tools.planned import _plan_budget, run_planned
from engine.tools.orchestrator import LoadedTool
from engine.tools.registry import build_tool
from tests.doubles import Judge, ScriptedLLM


def _mk(label):
    return LoadedTool(label=label, tool=build_tool("mock", {"tool_name": label, "output": f"{label} rows"}),
                      input_template={})


PLAN = json.dumps({"steps": [{"id": "s1", "intent": "i", "tool": "a", "input": {"query": "x"}, "dependencies": []}]})
TMPL = "You are the PLANNER.\n{tools}\n{skills}\n{max_steps}\n{task}\n{repair}\n{results}\n{failed}"


class _KwLLM(ScriptedLLM):
    """Records the kwargs of every call so the per-call budget is observable."""
    def __init__(self, **k):
        super().__init__(**k)
        self.kws = []

    def complete(self, prompt, **kw):
        self.kws.append(kw)
        return super().complete(prompt, **kw)


# --- the budget -----------------------------------------------------------------------------------------------
def test_plan_budget_defaults_to_8192_and_never_below_the_general_cap(monkeypatch):
    assert _plan_budget() == 8192
    monkeypatch.setenv("LLM_MAX_TOKENS", "16000")
    assert _plan_budget() == 16000                                   # general cap raised above -> follows it
    monkeypatch.setenv("PLAN_MAX_TOKENS", "20000")
    assert _plan_budget() == 20000
    monkeypatch.setenv("PLAN_MAX_TOKENS", "abc")
    with pytest.raises(ValueError):
        _plan_budget()


def test_planner_call_carries_its_own_max_tokens():
    llm = _KwLLM(plans=[PLAN])
    run_planned("q", [_mk("a")], llm, TMPL, TMPL)
    assert llm.kws and llm.kws[0] == {"max_tokens": 8192}           # the plan call, not the general cap


def test_max_tokens_override_is_honored_by_the_adapters_helper():
    assert _max_tokens(8192) == 8192 and _max_tokens() == 4096 and _max_tokens(0) == 1


def test_truncation_diagnostic_names_the_planner_knob():
    class _Truncating:
        def complete(self, prompt, **kw):
            raise RuntimeError("Cortex output truncated (hit max_tokens); raise LLM_MAX_TOKENS")
    out = run_planned("q", [_mk("a")], _Truncating(), TMPL, TMPL)
    assert "PLAN_MAX_TOKENS" in out and "8192" in out                 # actionable, not the generic message


# --- the optional planner model ------------------------------------------------------------------------------
def test_no_planner_configured_means_the_worker_plans():
    assert _resolve_planner(None) == (None, None)
    assert get_planner_client("dq_qals", {"worker": {"provider": "mock"}}) is None
    assert "planner=" not in model_summary({"worker": {"provider": "mock"}})


def test_pack_planner_profile_and_env_precedence(monkeypatch):
    cfg = {"worker": {"provider": "cortex", "model": "claude-opus-5"},
           "planner": {"provider": "cortex", "model": "claude-haiku-4-5"}}
    assert _resolve_planner(cfg) == ("cortex", "claude-haiku-4-5")
    assert "planner=cortex:claude-haiku-4-5" in model_summary(cfg)
    monkeypatch.setenv("PLANNER_PROVIDER", "mock")                   # env wins; the pack model belongs to cortex
    assert _resolve_planner(cfg) == ("mock", None)
    monkeypatch.setenv("PLANNER_MODEL", "m2")
    assert _resolve_planner(cfg) == ("mock", "m2")


def test_planned_graph_uses_the_planner_client_for_the_plan_and_the_worker_for_the_answer(monkeypatch):
    base = G.load_config("ops_rca")
    monkeypatch.setattr(G, "load_config", lambda uc: {**base, "max_iters": 0, "pass_score": 18,
                                                       "eval_retries": 0})
    plan = json.dumps({"steps": [{"id": "s1", "intent": "d", "tool": "detect", "input": {"query": "x"},
                                  "dependencies": []}]})
    planner = ScriptedLLM(plans=[plan])
    worker = ScriptedLLM(answers=["answer from worker"])
    monkeypatch.setattr(G, "get_llm_client", lambda uc, m: worker)
    monkeypatch.setattr(G, "get_planner_client", lambda uc, m: planner)
    final = build_graph("ops_rca", eval_llm=Judge(), verbose=False).invoke(initial_state("q"))
    assert any("PLANNER" in p for p in planner.prompts) and not any("PLANNER" in p for p in worker.prompts)
    assert final["best_answer"] == "answer from worker"
