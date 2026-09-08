"""
ENGINE / the loop.  GENERIC and platform-neutral.  Loads a USE-CASE PACK by name
and composes it with the shared/ tier:
  - skills:    shared/skills/*  +  pack/skills/*        (concatenated)
  - prompts:   pack/prompts/<f>  ELSE  shared/prompts/<f>  (pack overrides)
  - config:    shared defaults  +  pack config          (pack wins; via inherits:)
Runs generate -> evaluate -> refine.  Never changes per use case or platform.
"""
from __future__ import annotations
import os
import re
import json
import math
import uuid
from pathlib import Path
from typing import TypedDict

import yaml
from pydantic import BaseModel, ValidationError, field_validator
from langgraph.graph import StateGraph, END

from engine.llm_client import get_llm_client, get_eval_client, model_summary, LLMClient
from engine.sql_tool import get_sql_tool, SQLTool
from engine.tracing import instrument       # observability is applied from OUTSIDE the nodes

REPO_ROOT = Path(__file__).resolve().parents[1]
USECASES = REPO_ROOT / "usecases"
SHARED = REPO_ROOT / "shared"


def load_config(use_case: str) -> dict:
    """Merge inherited bases (e.g. shared) then the pack's own config (pack wins)."""
    pack = yaml.safe_load((USECASES / use_case / "config.yaml").read_text()) or {}
    inherits = pack.get("inherits", [])
    if isinstance(inherits, str):                      # `inherits: shared` -> ["shared"], not chars
        inherits = [inherits]
    merged: dict = {}
    for base in inherits:
        base_cfg = (SHARED if base == "shared" else REPO_ROOT / base) / "config.yaml"
        if base_cfg.exists():
            merged.update(yaml.safe_load(base_cfg.read_text()) or {})
    merged.update(pack)
    merged.pop("inherits", None)
    return merged


def _prompt(use_case: str, name: str) -> str:
    """Pack's prompt if present, else fall back to shared/ (override semantics)."""
    for base in (USECASES / use_case / "prompts", SHARED / "prompts"):
        f = base / name
        if f.exists():
            return f.read_text()
    raise FileNotFoundError(f"{name} not found in pack or shared")


def load_skills(use_case: str) -> str:
    """shared skills + pack skills, concatenated (additive)."""
    files = []
    for base in (SHARED / "skills", USECASES / use_case / "skills"):
        if base.exists():
            files += sorted(base.glob("*.md"))
    return "\n\n".join(f.read_text().strip() for f in files) if files else "None."


def load_exemplars(use_case: str) -> str:
    """Verified question -> SQL pairs (pack-specific), injected as few-shot."""
    d = USECASES / use_case / "exemplars"
    pairs = []
    if d.exists():
        for f in sorted(d.glob("*.yaml")):
            pairs += yaml.safe_load(f.read_text()) or []
    if not pairs:
        return "None."
    return "\n\n".join(f"Q: {p['question']}\nSQL: {p['sql']}" for p in pairs)


def fill(template: str, **kw) -> str:
    """Single-pass placeholder fill: replace each {key} with its kwarg in ONE pass, so a
    substituted value that itself contains "{other}" is never re-interpreted, and literal
    braces in SQL/JSON examples (or unknown {names}) are left untouched (brace-safe)."""
    return re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}",
                  lambda m: str(kw[m.group(1)]) if m.group(1) in kw else m.group(0),
                  template)


class Verdict(BaseModel):
    """Typed judge output: a bounded score plus the reason that drives `refine`.
    Replaces a brittle regex scrape with a validated object (see parse_verdict)."""
    score: int
    reason: str = ""

    @field_validator("score")
    @classmethod
    def _non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("score must be >= 0")
        return v


def _coerce_score(value) -> int:
    """A score must be a finite number; round to the nearest int (don't truncate 17.9->17)."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"score is not numeric: {value!r}")
    if not math.isfinite(f):
        raise ValueError("score must be finite")
    return round(f)


def parse_verdict(raw: str, max_score: int) -> Verdict:
    """Turn a judge reply into a validated Verdict. Accepts either a JSON object
    {"score": N, "reason": "..."} OR the house line form  SCORE: N/<max> - <reason>.
    Strict: rounds (not truncates) numeric scores, rejects a non-finite score, and rejects a
    mismatched denominator (so "18/100" is NOT read as 18/max). Injection defense lives in the
    rubric prompt (the candidate answer is delimited + marked untrusted) plus JSON-first parsing.
    Raises ValueError / ValidationError on anything unparseable or out of range (caller retries)."""
    text = (raw or "").strip()
    data: dict | None = None

    m = re.search(r"\{.*\}", text, re.DOTALL)          # 1) JSON object anywhere
    if m:
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict) and "score" in obj:
                data = {"score": _coerce_score(obj["score"]),
                        "reason": str(obj.get("reason", "")).strip()}
        except (ValueError, TypeError):
            data = None

    if data is None:                                   # 2) line form: SCORE: N[/denom] - reason
        m = re.search(r"SCORE:\s*([0-9]+(?:\.[0-9]+)?)\s*(?:/\s*([0-9]+))?\s*[-–—:]*\s*([^\n]*)",
                      text, re.IGNORECASE)
        if not m:
            raise ValueError(f"no score found in judge reply: {text[:120]!r}")
        denom = m.group(2)
        if denom is not None and int(denom) != max_score:
            raise ValueError(f"denominator {denom} != max_score {max_score}")
        data = {"score": _coerce_score(m.group(1)), "reason": m.group(3).strip() or text}

    verdict = Verdict(**data)
    if verdict.score > max_score:
        raise ValueError(f"score {verdict.score} exceeds max {max_score}")
    return verdict


class State(TypedDict):
    task: str
    data: str
    answer: str
    feedback: str
    score: int
    best_answer: str
    best_score: int
    iterations: int
    run_id: str               # per-run id; the join key for observability (see engine/tracing.py)


def build_graph(use_case: str, llm: LLMClient | None = None,
                sql: SQLTool | None = None, eval_llm: LLMClient | None = None,
                verbose: bool = True):
    cfg = load_config(use_case)
    # max_score = the rubric denominator / validation cap; pass_score = the stop threshold.
    # `threshold` is kept as the backward-compatible alias for pass_score.
    max_score = cfg.get("max_score", 18)
    pass_score = cfg.get("pass_score", cfg.get("threshold", max_score))
    max_iters = cfg.get("max_iters", 4)
    eval_retries = cfg.get("eval_retries", 1)          # extra judge re-asks on unparseable output
    for _name, _val, _min in (("max_score", max_score, 1), ("pass_score", pass_score, 0),
                              ("max_iters", max_iters, 0), ("eval_retries", eval_retries, 0)):
        if type(_val) is not int or _val < _min:       # `type is not int` also rejects bools
            raise ValueError(f"{_name} must be an integer >= {_min}")
    if pass_score > max_score:
        raise ValueError("pass_score must be <= max_score")
    if max_iters > 10:                                 # keep graph steps under LangGraph's default recursion limit (25)
        raise ValueError("max_iters must be <= 10 (raise LangGraph's recursion_limit if you truly need more)")
    retry_nudge = (f"\n\nYour previous reply could not be parsed. Reply with EXACTLY "
                   f"one line:  SCORE: N/{max_score} - <short reason>")

    injected_llm = llm                                 # remember whether a worker was injected
    llm = llm or get_llm_client(use_case)             # worker: generate + refine
    # judge: injected eval wins; else reuse an injected worker (so a real worker isn't paired
    # with a mock judge); else resolve independently from env.
    eval_llm = eval_llm or injected_llm or get_eval_client(use_case)
    sql = sql or get_sql_tool(use_case, cfg.get("default_sql_tool", "mock"))
    skills = load_skills(use_case)
    exemplars = load_exemplars(use_case)

    def log(m):
        if verbose:
            print(m)

    log(f"  [models]   {model_summary()}")

    def generate(s: State) -> State:
        data = sql.ask(s["task"])
        answer = llm.complete(fill(_prompt(use_case, "generate.md"),
                                   task=s["task"], data=data,
                                   skills=skills, exemplars=exemplars, revision=0))
        log(f"  [generate] rev0 -> {answer}")
        return {**s, "data": data, "answer": answer, "iterations": 0}

    def evaluate(s: State) -> State:
        # Ground the judge: give it the DATA the answer must be consistent with, so a
        # fabricated number can't satisfy the rubric (the judge can check against evidence).
        base = fill(_prompt(use_case, "rubric.md"),
                    task=s["task"], answer=s["answer"], data=s.get("data", ""))
        verdict: Verdict | None = None
        for attempt in range(eval_retries + 1):
            raw = eval_llm.complete(base if attempt == 0 else base + retry_nudge)
            try:
                verdict = parse_verdict(raw, max_score)
                break
            except (ValueError, ValidationError) as e:
                log(f"  [evaluate] unparseable verdict "
                    f"(attempt {attempt + 1}/{eval_retries + 1}): {e}")
        if verdict is None:                            # retries exhausted -> safe worst case
            verdict = Verdict(score=0,
                              reason="judge output unparseable; scored 0 to force a refine")
        score = verdict.score
        feedback = f"SCORE: {score}/{max_score} - {verdict.reason}"
        log(f"  [evaluate] score {score}/{max_score}  ({verdict.reason})")
        best_a, best_s = (s["answer"], score) if score > s.get("best_score", -1) \
            else (s["best_answer"], s["best_score"])
        return {**s, "score": score, "feedback": feedback,
                "best_answer": best_a, "best_score": best_s}

    def refine(s: State) -> State:
        nxt = s["iterations"] + 1
        answer = llm.complete(fill(_prompt(use_case, "refine.md"),
                                   task=s["task"], answer=s["answer"],
                                   feedback=s["feedback"], skills=skills, revision=nxt))
        log(f"  [refine]   rev{nxt} -> {answer}")
        return {**s, "answer": answer, "iterations": nxt}

    def keep_going(s: State) -> str:
        return "stop" if (s["score"] >= pass_score or s["iterations"] >= max_iters) else "refine"

    # Observability is applied here, from OUTSIDE the nodes: instrument() wraps each node
    # to emit a timed event (or returns it unchanged when TRACER=none -> zero overhead).
    g = StateGraph(State)
    g.add_node("generate", instrument("generate", generate, use_case))
    g.add_node("evaluate", instrument("evaluate", evaluate, use_case))
    g.add_node("refine", instrument("refine", refine, use_case))
    g.set_entry_point("generate")
    g.add_edge("generate", "evaluate")
    g.add_conditional_edges("evaluate", keep_going, {"refine": "refine", "stop": END})
    g.add_edge("refine", "evaluate")
    return g.compile()


def initial_state(task: str) -> State:
    return {"task": task, "data": "", "answer": "", "feedback": "",
            "score": -1, "best_answer": "", "best_score": -1, "iterations": 0,
            "run_id": uuid.uuid4().hex[:12]}
