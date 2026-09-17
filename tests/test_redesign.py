"""The redesigned core: typed config, typed retrieval, one dispatch-guarded SQL path, node-level
behavior, and the two loop-contract seams (declared output schema; judge verification tool).
All offline; every guarantee here is a §3 property from the architect brief, re-proven on the new shape."""
import pytest
from pydantic import ValidationError

import engine.graph as G
from engine.config import PackConfig
from engine.graph import build_graph, initial_state
from engine.retrieval import Retrieval, SqlRetriever
from engine.sql_tool import no_data, is_no_data
from engine.tools import gather_context, build_tool
from engine.tools.orchestrator import LoadedTool
from tests.doubles import Cap, Judge, Sql

_CFG = {"max_score": 18, "pass_score": 18, "max_iters": 0, "eval_retries": 0, "max_stall": 0,
        "default_sql_tool": "mock"}


# --- PackConfig: typos and bad bounds fail at build time, not silently -------------------------------
def test_config_rejects_unknown_key_typo():
    with pytest.raises(ValidationError) as e:
        PackConfig.from_dict({**_CFG, "max_plan_step": 3})       # typo (missing 's') -> error, not no-op
    assert "max_plan_step" in str(e.value)


@pytest.mark.parametrize("bad", [
    {"max_iters": 11}, {"max_plan_steps": 21}, {"max_replans": 6}, {"max_data_retries": 6},
    {"max_ref_chars": 100}, {"max_score": 0}, {"pass_score": 19, "max_score": 18},
    {"max_iters": True}, {"tool_mode": "magic"}, {"zero_is_no_data": "maybe"},
    {"recover_value_dims": "not-a-list"},
])
def test_config_rejects_bad_values(bad):
    with pytest.raises((ValidationError, ValueError)):
        PackConfig.from_dict({**_CFG, **bad})


def test_config_threshold_is_the_pass_score_alias_and_defaults():
    c = PackConfig.from_dict({"threshold": 15})
    assert c.pass_score == 15 and c.max_score == 18
    assert PackConfig.from_dict({}).pass_score == 18            # default = max_score
    assert PackConfig.from_dict({"zero_is_no_data": "true"}).zero_is_no_data is True   # strict token ok


def test_config_remembers_whether_tools_key_was_declared():
    assert PackConfig.from_dict({}).tools_declared is False       # absent -> legacy SQL path
    assert PackConfig.from_dict({"tools": []}).tools_declared is True   # [] -> toolless


# --- Retrieval: groundedness is typed; the sentinel is interpreted in ONE place -----------------------
def test_retrieval_from_text_interprets_the_sentinel_once():
    r = Retrieval.from_text(no_data("why"))
    assert r.empty and r.hint == "why" and r.text == "why"
    assert not Retrieval.from_text("col | val\nA | 1").empty
    assert Retrieval.from_text("").empty and Retrieval.from_text(None).empty


def test_gather_context_does_not_count_a_sentinel_as_evidence():
    # the latent bug: a tool that "ran fine" but returned NO_DATA used to count as usable output
    lt = LoadedTool(label="t", tool=build_tool("mock", {"tool_name": "t", "output": no_data("nothing")}),
                    input_template={})
    assert is_no_data(gather_context("q", [lt], "rid"))


# --- the single-SQL path now runs through dispatch(): bounded, and a hard error is still fatal --------
def test_legacy_sql_path_is_bounded_by_dispatch(monkeypatch):
    monkeypatch.setattr(G, "load_config", lambda uc: dict(_CFG))
    big = Sql("x" * 50_000)                                       # a runaway result
    final = build_graph("dq_qals", llm=Cap("a"), eval_llm=Judge(), sql=big, verbose=False) \
        .invoke(initial_state("q"))
    assert len(final["data"]) < 50_000 and "truncated" in final["data"]   # dispatch bound applied


def test_first_retrieval_hard_error_is_fatal(monkeypatch):
    monkeypatch.setattr(G, "load_config", lambda uc: dict(_CFG))
    boom = Sql(RuntimeError("bad SQL"))
    with pytest.raises(RuntimeError):
        build_graph("dq_qals", llm=Cap("a"), eval_llm=Judge(), sql=boom, verbose=False) \
            .invoke(initial_state("q"))


def test_sql_retriever_strategy_flags():
    r = SqlRetriever(Sql("rows"))
    assert r.frames_question and r.reformulates
    assert not r.retrieve("q").empty


# --- seam: declared output schema is a hard contract the loop enforces ---------------------------------
def test_output_schema_violation_scores_zero_then_refine_fixes_shape(monkeypatch):
    monkeypatch.setattr(G, "load_config", lambda uc: {
        **_CFG, "max_iters": 1, "output_schema": {"type": "object", "required": ["total"],
                                                  "properties": {"total": {"type": "number"}}}})
    worker = Cap("Total is 42.", '{"total": 42, "note": "ok"}')   # prose first, valid JSON after refine
    judge = Judge("SCORE: 18/18 - ok")
    final = build_graph("dq_qals", llm=worker, eval_llm=judge, sql=Sql("t | v\nA | 42"),
                        verbose=False).invoke(initial_state("q"))
    assert judge.calls == 1                                       # the violating draft never reached the judge
    assert final["best_score"] == 18 and final["best_answer"].startswith("{")


def test_output_schema_type_mismatch_is_a_violation(monkeypatch):
    from engine.output import validate_output
    ok, _, err = validate_output('{"total": "42"}', {"required": ["total"], "properties": {"total": {"type": "number"}}})
    assert not ok and "total" in err
    ok, _, _ = validate_output('{"total": true}', {"properties": {"total": {"type": "number"}}})
    assert not ok                                                  # a bool is not a number
    assert validate_output("anything", None) == (True, None, "")   # undeclared -> pass-through


# --- seam: the judge may spot-check with ONE declared read-only tool --------------------------------------
_TOOLS = [{"type": "mock", "name": "verifier", "input": {"query": "${task}"},
           "params": {"output": "VERIFIED total=42"}}]


def test_judge_verify_tool_feeds_the_rubric(monkeypatch):
    monkeypatch.setattr(G, "load_config", lambda uc: {**_CFG, "tools": _TOOLS, "judge_verify_tool": "verifier"})
    judge = Judge("SCORE: 18/18 - matches verification")
    final = build_graph("dq_qals", llm=Cap("total is 42"), eval_llm=judge, verbose=False) \
        .invoke(initial_state("q"))
    assert final["best_score"] == 18
    assert any("VERIFICATION" in p and "VERIFIED total=42" in p for p in judge.prompts)


def test_judge_verify_tool_must_be_read_only(monkeypatch):
    writer = [{"type": "mock", "name": "w", "params": {"output": "x", "read_only": False}}]
    monkeypatch.setattr(G, "load_config", lambda uc: {**_CFG, "tools": writer, "judge_verify_tool": "w"})
    with pytest.raises(ValueError):
        build_graph("dq_qals", llm=Cap("a"), eval_llm=Judge(), verbose=False)


# --- seam: a session id travels with the run (multi-turn is a future capability) --------------------------
def test_thread_id_seam():
    assert initial_state("q")["thread_id"] == ""
    assert initial_state("q", thread_id="t1")["thread_id"] == "t1"
