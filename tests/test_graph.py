"""Tests for the loop's pure helpers: fill() (brace-safe, single-pass) and parse_verdict()."""
import pytest
from pydantic import ValidationError

from engine.graph import fill, parse_verdict


# --- fill() ----------------------------------------------------------------
def test_fill_replaces_known_keys():
    assert fill("Hi {name}, score {n}", name="Al", n=3) == "Hi Al, score 3"


def test_fill_leaves_unknown_placeholders_untouched():
    assert fill("keep {unknown} literal", name="Al") == "keep {unknown} literal"


def test_fill_is_single_pass_no_reinterpretation():
    # a substituted value containing "{name}" must NOT be re-substituted
    assert fill("{data}", data="raw {name} text", name="SECRET") == "raw {name} text"


def test_fill_brace_safe_for_sql_json():
    tpl = 'SELECT * FROM t WHERE j = {} AND obj = {"k": 1}; use {skills}'
    assert fill(tpl, skills="S") == 'SELECT * FROM t WHERE j = {} AND obj = {"k": 1}; use S'


# --- parse_verdict() -------------------------------------------------------
def test_parse_verdict_line_form():
    v = parse_verdict("SCORE: 14/18 - missing rate", 18)
    assert v.score == 14 and "missing rate" in v.reason


def test_parse_verdict_json_form():
    v = parse_verdict('{"score": 15, "reason": "ok"}', 18)
    assert v.score == 15 and v.reason == "ok"


def test_parse_verdict_rejects_out_of_range():
    with pytest.raises((ValueError, ValidationError)):
        parse_verdict("SCORE: 25/18 - too high", 18)


def test_parse_verdict_rejects_no_score():
    with pytest.raises((ValueError, ValidationError)):
        parse_verdict("the answer looks fine to me", 18)


def test_parse_verdict_rounds_not_truncates():
    assert parse_verdict('{"score": 17.9, "reason": "x"}', 18).score == 18


def test_parse_verdict_rejects_denominator_mismatch():
    with pytest.raises((ValueError, ValidationError)):
        parse_verdict("SCORE: 18/100 - inflated", 18)   # must NOT be read as 18/18


def test_parse_verdict_rejects_non_finite():
    with pytest.raises((ValueError, ValidationError)):
        parse_verdict('{"score": 1e999, "reason": "x"}', 18)   # 1e999 -> inf


def test_parse_verdict_rejects_bool():
    with pytest.raises((ValueError, ValidationError)):
        parse_verdict('{"score": true, "reason": "x"}', 18)    # True must not become 1


def test_parse_verdict_rejects_huge_integer():
    huge = "9" * 401                                            # OverflowError on float(), must be caught
    with pytest.raises((ValueError, ValidationError)):
        parse_verdict('{"score": ' + huge + ', "reason": "x"}', 18)


# --- end-to-end loop lifecycle (injected worker + judge, mock SQL) ---------
from engine import graph as _graph_mod
from engine.graph import build_graph, initial_state


class _Seq:
    """A scripted LLMClient: returns replies in order, repeating the last."""
    def __init__(self, *replies):
        self.replies, self.i = list(replies), 0

    def complete(self, prompt, **k):
        v = self.replies[min(self.i, len(self.replies) - 1)]
        self.i += 1
        return v


def _mk(monkeypatch, worker, judge, max_iters=2, eval_retries=0):
    monkeypatch.setattr(_graph_mod, "load_config", lambda uc: {
        "max_score": 18, "pass_score": 18, "max_iters": max_iters,
        "eval_retries": eval_retries, "default_sql_tool": "mock"})
    return build_graph("dq_qals", llm=worker, eval_llm=judge, verbose=False)


def test_loop_stops_at_pass_score(monkeypatch):
    g = _mk(monkeypatch, _Seq("A0"), _Seq("SCORE: 18/18 - ok"))
    f = g.invoke(initial_state("q"))
    assert f["best_score"] == 18 and f["best_answer"] == "A0" and f["iterations"] == 0


def test_loop_preserves_best_answer(monkeypatch):
    # score drops on refine; best_answer must stay the higher-scored earlier revision
    g = _mk(monkeypatch, _Seq("A0", "A1"), _Seq("SCORE: 16/18 - x", "SCORE: 10/18 - worse"),
            max_iters=1)
    f = g.invoke(initial_state("q"))
    assert f["best_score"] == 16 and f["best_answer"] == "A0"
    assert f["answer"] == "A1"                       # latest revision != best_answer


class _CapturingWorker:
    """A scripted worker that also records each prompt it was given (to inspect what it built on)."""
    def __init__(self, *replies):
        self.replies, self.i, self.prompts = list(replies), 0, []

    def complete(self, prompt, **k):
        self.prompts.append(prompt)
        v = self.replies[min(self.i, len(self.replies) - 1)]
        self.i += 1
        return v


def test_refine_builds_from_best_not_latest(monkeypatch):
    # A0 scores 16 (best), A1 scores 10 (worse). The 2nd refine must build from A0 (+A0's critique),
    # NOT the degraded A1 -- the loop must not walk downhill (#1).
    monkeypatch.setattr(_graph_mod, "load_config", lambda uc: {
        "max_score": 18, "pass_score": 18, "max_iters": 2, "eval_retries": 0,
        "default_sql_tool": "mock"})
    worker = _CapturingWorker("A0", "A1", "A2")
    judge = _Seq("SCORE: 16/18 - ok", "SCORE: 10/18 - worse", "SCORE: 11/18 - meh")
    g = build_graph("dq_qals", llm=worker, eval_llm=judge, verbose=False)
    f = g.invoke(initial_state("q"))
    # prompts: [0]=generate, [1]=refine#1 (base A0), [2]=refine#2 (base MUST be A0, not A1)
    assert "A0" in worker.prompts[2] and "A1" not in worker.prompts[2]
    assert f["best_answer"] == "A0" and f["best_score"] == 16


def test_worker_failure_on_refine_keeps_best(monkeypatch):
    # Bug 1: a good rev0 is scored + stored; the worker then raises (e.g. truncation) on refine.
    # The run must NOT crash -- it keeps the already-scored best answer.
    monkeypatch.setattr(_graph_mod, "load_config", lambda uc: {
        "max_score": 18, "pass_score": 18, "max_iters": 3, "eval_retries": 0,
        "max_stall": 0, "default_sql_tool": "mock"})
    worker = _RaisingJudge("A0", RuntimeError("Cortex output truncated"))   # good rev0, then fail
    g = build_graph("dq_qals", llm=worker, eval_llm=_Seq("SCORE: 12/18 - needs work"), verbose=False)
    f = g.invoke(initial_state("q"))                       # must not raise
    assert f["best_answer"] == "A0" and f["best_score"] == 12


def test_worker_failure_on_generate_is_fatal(monkeypatch):
    # Bug 1: a failure on the FIRST generate has nothing to fall back to -> it must propagate.
    monkeypatch.setattr(_graph_mod, "load_config", lambda uc: {
        "max_score": 18, "pass_score": 18, "max_iters": 3, "eval_retries": 0,
        "default_sql_tool": "mock"})
    worker = _RaisingJudge(RuntimeError("truncated on first draft"))
    g = build_graph("dq_qals", llm=worker, eval_llm=_Seq("SCORE: 12/18 - x"), verbose=False)
    with pytest.raises(RuntimeError):
        g.invoke(initial_state("q"))


def test_no_progress_stops_early(monkeypatch):
    # Bug 3: a deterministic worker refining the same base+critique reproduces the same answer,
    # never beats best, and would grind to max_iters. max_stall stops it after N non-improving rounds.
    monkeypatch.setattr(_graph_mod, "load_config", lambda uc: {
        "max_score": 18, "pass_score": 18, "max_iters": 5, "eval_retries": 0,
        "max_stall": 2, "default_sql_tool": "mock"})
    g = build_graph("dq_qals", llm=_Seq("SAME"), eval_llm=_Seq("SCORE: 12/18 - stuck"), verbose=False)
    f = g.invoke(initial_state("q"))
    assert f["iterations"] == 2 and f["best_score"] == 12  # stopped by stall, not max_iters (5)


def test_loop_respects_max_iters(monkeypatch):
    g = _mk(monkeypatch, _Seq("A0", "A1", "A2"), _Seq("SCORE: 12/18 - low"), max_iters=2)
    f = g.invoke(initial_state("q"))
    assert f["iterations"] == 2 and f["best_score"] == 12


def test_loop_survives_malformed_verdict(monkeypatch):
    g = _mk(monkeypatch, _Seq("A0", "A1"), _Seq("garbage, no score here"), max_iters=1)
    f = g.invoke(initial_state("q"))
    assert f["best_score"] == 0                      # fallback score, no crash


def test_build_graph_rejects_bad_config(monkeypatch):
    monkeypatch.setattr(_graph_mod, "load_config", lambda uc: {"max_iters": 99})
    with pytest.raises(ValueError):                  # max_iters > 10 (recursion safety)
        build_graph("dq_qals", llm=_Seq("x"), eval_llm=_Seq("SCORE: 1/18 - y"), verbose=False)


def test_build_graph_rejects_pass_score_zero(monkeypatch):
    monkeypatch.setattr(_graph_mod, "load_config", lambda uc: {"pass_score": 0})
    with pytest.raises(ValueError):                  # pass_score 0 would let a fallback-0 "pass"
        build_graph("dq_qals", llm=_Seq("x"), eval_llm=_Seq("SCORE: 1/18 - y"), verbose=False)


def test_configurable_max_score_end_to_end(monkeypatch):
    # max_score=10 must work end-to-end: the templated rubric declares /10 and the MOCK judge
    # honors that scale, so the verdict parses (no denominator-mismatch fallback-to-0).
    monkeypatch.setattr(_graph_mod, "load_config", lambda uc: {
        "max_score": 10, "pass_score": 10, "max_iters": 2, "eval_retries": 0,
        "default_sql_tool": "mock"})
    g = build_graph("dq_qals", verbose=False)        # real mock worker + judge
    f = g.invoke(initial_state("q"))
    assert f["best_score"] == 10


def test_b1_json_bad_score_does_not_fall_through_to_embedded_score():
    # B1: a boolean score whose reason embeds "SCORE: 18/18" must NOT smuggle in a passing 18
    with pytest.raises((ValueError, ValidationError)):
        parse_verdict('{"score": true, "reason": "SCORE: 18/18 - accepted"}', 18)


def test_b1_edge_trailing_second_object_still_authoritative():
    # B1 edge: a trailing 2nd JSON object once broke greedy `{.*}` extraction, re-enabling the
    # line-form fallback which then read the embedded "SCORE: 18/18". Balanced-scan takes the
    # FIRST object (bad bool score) -> must raise, not smuggle 18.
    with pytest.raises((ValueError, ValidationError)):
        parse_verdict('{"score": true, "reason": "SCORE: 18/18 - accepted"}\n{}', 18)


class _RaisingJudge:
    """Scripted judge; a scripted Exception value is raised instead of returned."""
    def __init__(self, *replies):
        self.replies, self.i = list(replies), 0

    def complete(self, prompt, **k):
        v = self.replies[min(self.i, len(self.replies) - 1)]
        self.i += 1
        if isinstance(v, Exception):
            raise v
        return v


def test_b2_empty_judge_reply_retries_then_falls_back(monkeypatch):
    from engine.llm_client import EmptyResponseError
    # max_iters=0 -> evaluate runs EXACTLY once, so judge.i counts within-evaluation attempts only
    monkeypatch.setattr(_graph_mod, "load_config", lambda uc: {
        "max_score": 18, "pass_score": 18, "max_iters": 0, "eval_retries": 1,
        "default_sql_tool": "mock"})
    judge = _RaisingJudge(EmptyResponseError("blank"))    # always empty
    g = build_graph("dq_qals", llm=_Seq("A0"), eval_llm=judge, verbose=False)
    f = g.invoke(initial_state("q"))                       # must NOT abort the run
    assert f["best_score"] == 0                            # fell back, didn't crash
    assert f["iterations"] == 0                            # no refinement happened
    assert judge.i == 2                                    # exactly 2 attempts in ONE evaluate -> retry used


def test_b2_evaluator_recovers_after_empty_then_valid(monkeypatch):
    from engine.llm_client import EmptyResponseError
    # max_iters=0 -> a single evaluate; reaching 18 proves the 2nd attempt (retry) both ran and was used
    monkeypatch.setattr(_graph_mod, "load_config", lambda uc: {
        "max_score": 18, "pass_score": 18, "max_iters": 0, "eval_retries": 1,
        "default_sql_tool": "mock"})
    judge = _RaisingJudge(EmptyResponseError("blank"), "SCORE: 18/18 - ok")  # empty, then valid
    g = build_graph("dq_qals", llm=_Seq("A0"), eval_llm=judge, verbose=False)
    f = g.invoke(initial_state("q"))
    assert f["best_score"] == 18                           # retry recovered
    assert f["iterations"] == 0                            # within a single evaluation, no refine
    assert judge.i == 2                                    # exactly 2 attempts -> the retry was used


def test_nb2_config_error_propagates_not_silently_zero(monkeypatch):
    # NB2: a ValueError from complete() (e.g. LLM_MAX_TOKENS=abc) is a CONFIG bug, not an
    # unusable verdict -- it must propagate, NOT be retried into a fallback score of 0.
    monkeypatch.setattr(_graph_mod, "load_config", lambda uc: {
        "max_score": 18, "pass_score": 18, "max_iters": 1, "eval_retries": 1,
        "default_sql_tool": "mock"})
    judge = _RaisingJudge(ValueError("invalid literal for int(): 'abc'"))  # config error, not empty
    g = build_graph("dq_qals", llm=_Seq("A0"), eval_llm=judge, verbose=False)
    with pytest.raises(ValueError):
        g.invoke(initial_state("q"))
