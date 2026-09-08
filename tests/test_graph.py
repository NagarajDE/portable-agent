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
