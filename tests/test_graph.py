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
    monkeypatch.setattr(_graph_mod, "load_config", lambda uc: {
        "max_score": 18, "pass_score": 18, "max_iters": 1, "eval_retries": 1,
        "default_sql_tool": "mock"})
    judge = _RaisingJudge(EmptyResponseError("blank"))    # always empty
    g = build_graph("dq_qals", llm=_Seq("A0", "A1"), eval_llm=judge, verbose=False)
    f = g.invoke(initial_state("q"))                       # must NOT abort the run
    assert f["best_score"] == 0                            # fell back, didn't crash


def test_b2_evaluator_recovers_after_empty_then_valid(monkeypatch):
    from engine.llm_client import EmptyResponseError
    monkeypatch.setattr(_graph_mod, "load_config", lambda uc: {
        "max_score": 18, "pass_score": 18, "max_iters": 1, "eval_retries": 1,
        "default_sql_tool": "mock"})
    judge = _RaisingJudge(EmptyResponseError("blank"), "SCORE: 18/18 - ok")  # empty, then valid
    g = build_graph("dq_qals", llm=_Seq("A0"), eval_llm=judge, verbose=False)
    f = g.invoke(initial_state("q"))
    assert f["best_score"] == 18                           # retry recovered
