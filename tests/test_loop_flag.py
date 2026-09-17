"""R1 -- the pack-level `loop:` flag. Default NON-LOOP: the worker's answer is final and UNSCORED; framing,
retrieval (incl. planned multi-step), reformulation and the no-data escalation still run. `loop: true`
restores generate -> evaluate -> refine. A truthy flag with no buildable evaluator warns and runs non-loop.
All offline."""
import json
import warnings

import pytest

import engine.graph as G
from engine.config import PackConfig, loop_flag
from engine.graph import build_graph, initial_state, USECASES
from engine.sql_tool import no_data
from tests.doubles import Cap, Judge, Sql, ScriptedLLM

_BASE = {"max_score": 18, "pass_score": 18, "max_iters": 2, "eval_retries": 0, "max_stall": 0,
         "default_sql_tool": "mock"}


# --- the flag's parsing contract ----------------------------------------------------------------------
@pytest.mark.parametrize("raw", [None, "", "   ", False, "false", "no", "0"])
def test_missing_null_blank_false_mean_non_loop(raw):
    assert loop_flag(raw) is False
    assert PackConfig.from_dict({"loop": raw}).loop is False


def test_absent_key_is_non_loop_even_with_models_configured():
    cfg = PackConfig.from_dict({"models": {"worker": {"provider": "mock"}, "evaluator": {"provider": "mock"}}})
    assert cfg.loop is False


@pytest.mark.parametrize("raw", [True, "true", "yes", "1", "on"])
def test_truthy_turns_the_loop_on(raw):
    assert PackConfig.from_dict({"loop": raw}).loop is True


def test_typo_is_an_error_not_a_guess():
    with pytest.raises(ValueError):
        PackConfig.from_dict({"loop": "maybe"})


# --- non-loop runs: one worker generation, judge never called, unscored ----------------------------------
def test_toolless_pack_without_flag_is_one_generation_unscored(monkeypatch):
    monkeypatch.setattr(G, "load_config", lambda uc: {**_BASE, "tools": []})
    w, j = Cap("final answer"), Judge()
    final = build_graph("dq_qals", llm=w, eval_llm=j, verbose=False).invoke(initial_state("q"))
    assert j.calls == 0                                     # no judge
    assert len(w.prompts) == 1                              # exactly one worker call (no refine)
    assert final["best_answer"] == "final answer" and final["answer"] == "final answer"
    assert final["best_score"] == -1 and final["score"] == -1   # unscored
    assert final["status"] == "" and final["grounded"] is True   # usable, not escalated


def test_sql_pack_without_flag_still_retrieves_then_answers_once(monkeypatch):
    monkeypatch.setattr(G, "load_config", lambda uc: dict(_BASE))
    sql, w, j = Sql("SUPPLIER | TOTAL\nIBM | 9"), Cap("IBM leads at 9."), Judge()
    final = build_graph("dq_qals", llm=w, eval_llm=j, sql=sql, verbose=False).invoke(initial_state("totals"))
    assert sql.calls == ["totals"] and "IBM | 9" in w.prompts[0]   # gathering happened, rows reached the worker
    assert j.calls == 0 and final["best_score"] == -1 and final["best_answer"] == "IBM leads at 9."


def test_planned_pack_without_flag_runs_the_multi_step_plan_then_one_answer(monkeypatch):
    base = G.load_config("ops_rca")
    monkeypatch.setattr(G, "load_config", lambda uc: {**base, "loop": None})   # explicit null -> non-loop
    plan = json.dumps({"steps": [
        {"id": "s1", "intent": "detect", "tool": "detect", "input": {"query": "failing"}, "dependencies": []},
        {"id": "s2", "intent": "health", "tool": "health", "input": {"query": "{{s1}}"}, "dependencies": ["s1"]}]})
    llm, j = ScriptedLLM(plans=[plan], answers=["orders-api is degraded."]), Judge()
    final = build_graph("ops_rca", llm=llm, eval_llm=j, verbose=False).invoke(initial_state("q"))
    assert "[s1 detect]" in final["data"] and "[s2 health]" in final["data"]   # both plan steps executed
    assert j.calls == 0 and final["best_score"] == -1                            # unscored
    assert final["best_answer"] == "orders-api is degraded."
    answer_prompts = [p for p in llm.prompts if "PLANNER" not in p and "REVISING" not in p]
    assert len(answer_prompts) == 1                                              # one synthesis, no refine


def test_non_loop_still_escalates_a_blank_retrieval(monkeypatch):
    # non-loop removes the judge, NOT the deterministic grounding: a blank still reaches a human as no_data
    monkeypatch.setattr(G, "load_config", lambda uc: {**_BASE, "max_data_retries": 0})
    final = build_graph("dq_qals", llm=Cap("honest: nothing"), eval_llm=Judge(), sql=Sql(no_data("none")),
                        verbose=False).invoke(initial_state("q"))
    assert final["status"] == "no_data" and final["grounded"] is False and final["best_score"] == -1


# --- flag on: the loop is exactly what it was ---------------------------------------------------------------
def test_flag_restores_the_full_loop(monkeypatch):
    monkeypatch.setattr(G, "load_config", lambda uc: {**_BASE, "loop": True})
    w, j = Cap("A0", "A1"), Judge("SCORE: 10/18 - meh")
    final = build_graph("dq_qals", llm=w, eval_llm=j, sql=Sql(), verbose=False).invoke(initial_state("q"))
    assert j.calls == 3 and final["iterations"] == 2 and final["best_score"] == 10   # evaluate + 2 refines


def test_flag_without_a_buildable_evaluator_warns_and_runs_non_loop(monkeypatch):
    # worker = mock (env default); evaluator = litellm with NO model -> LiteLLMClient raises ValueError
    monkeypatch.setattr(G, "load_config", lambda uc: {**_BASE, "loop": True, "tools": [],
                                                       "models": {"evaluator": {"provider": "litellm"}}})
    with pytest.warns(RuntimeWarning, match="no evaluator could be built"):
        g = build_graph("dq_qals", verbose=False)             # never fails the build
    final = g.invoke(initial_state("q"))
    assert final["best_score"] == -1 and final["status"] == "" and final["best_answer"]   # unscored, answered


def test_flag_with_a_buildable_evaluator_does_not_warn(monkeypatch):
    monkeypatch.setattr(G, "load_config", lambda uc: {**_BASE, "loop": True, "tools": []})
    with warnings.catch_warnings():
        warnings.simplefilter("error")                        # any warning would fail the test
        final = build_graph("dq_qals", verbose=False).invoke(initial_state("q"))   # mock worker + mock judge
    assert final["best_score"] >= 0


# --- migration: every shipped pack keeps scoring (behaves exactly as before) ---------------------------------
def _shipped_packs():
    return sorted(p.name for p in USECASES.iterdir() if p.is_dir() and not p.name.startswith("_")
                  and (p / "config.yaml").exists())


@pytest.mark.parametrize("uc", _shipped_packs())
def test_every_existing_pack_sets_loop_true(uc):
    assert PackConfig.from_dict(G.load_config(uc)).loop is True


def test_template_sets_loop_true():
    assert PackConfig.from_dict(G.load_config("_TEMPLATE")).loop is True


# --- the unscored-output contract at the surfaces ----------------------------------------------------------
def test_snowflake_shell_reports_score_none_status_ok_for_a_non_loop_pack(monkeypatch):
    import importlib
    from fastapi.testclient import TestClient
    monkeypatch.setenv("WORKER_PROVIDER", "mock")
    monkeypatch.setenv("SQL_TOOL", "mock")
    monkeypatch.setenv("TRACER", "none")
    real = G.load_config
    monkeypatch.setattr(G, "load_config", lambda uc: {**real(uc), "loop": False})
    from engine.platform_snowflake import agent
    importlib.reload(agent)
    r = TestClient(agent.app).post("/invoke", json={"question": "duplicate lots?"}).json()
    assert r["status"] == "ok" and r["score"] is None and r["grounded"] is True and r["answer"]
    monkeypatch.setattr(G, "load_config", real)
    importlib.reload(agent)                                   # leave the module as the other shell tests expect


def test_run_end_trace_event_marks_scored(monkeypatch):
    import engine.tracing as T
    events = []

    class _Rec:
        def event(self, name, **f):
            events.append({"event": name, **f})
        def flush(self): ...
        def close(self): ...

    monkeypatch.setenv("TRACER", "stdout")
    monkeypatch.setattr(T, "get_tracer", lambda run_id, uc: _Rec())    # capture, don't parse stdout
    monkeypatch.setattr(G, "load_config", lambda uc: {**_BASE, "tools": []})
    g = build_graph("dq_qals", llm=Cap("a"), eval_llm=Judge(), verbose=False)
    T.traced_invoke(g, initial_state("q"), "dq_qals")
    end = next(e for e in events if e["event"] == "run_end")
    assert end["scored"] is False and end["status"] == "ok" and end["best_score"] == -1
    assert not any(e["event"] == "evaluate" for e in events)   # the judge node never ran
