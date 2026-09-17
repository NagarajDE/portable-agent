"""
ENGINE / nodes -- the loop's three nodes (generate · evaluate · refine) and its routers, as PLAIN
FUNCTIONS of an explicit `Runtime`. No closures over a 300-line factory: every dependency a node uses
is a field on `Runtime`, so a node is unit-testable with doubles and readable on its own.

The guarantees these nodes carry (see CLAUDE.md) -- refine-from-best, grounded + validated scoring,
escalate-never-fabricate, failed-refine-is-non-fatal, no-progress stop -- are unchanged in meaning;
only the mechanism is now explicit.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from pydantic import ValidationError

from engine.config import PackConfig
from engine.guards import _REFUSE, rewrite_drift
from engine.llm_client import LLMClient, EmptyResponseError
from engine.output import validate_output
from engine.packs import _fill, _instr_block
from engine.retrieval import Retriever, Retrieval
from engine.tools.base import ToolContext
from engine.tools.dispatch import dispatch
from engine.tools.orchestrator import LoadedTool, _render
from engine.verdict import Verdict, parse_verdict


@dataclass
class Runtime:
    """Everything the nodes need, built ONCE by the composition root (graph.build_graph)."""
    use_case: str
    cfg: PackConfig
    llm: LLMClient                    # worker: frames, reformulates, generates, refines
    eval_llm: LLMClient               # judge
    retriever: Retriever
    prompt: Callable[[str], str]      # name -> template (pack override else shared)
    skills: str
    frame_skills: str
    exemplars: str
    tools_desc: str
    base_instructions: str
    verify_tool: LoadedTool | None    # opt-in: a read-only tool the judge may spot-check with
    tool_timeout_s: float
    log: Callable[[str], None]

    def instructions_for(self, s: dict) -> str:
        runtime = s.get("instructions", "") if self.cfg.allow_runtime_instructions else ""
        return _instr_block(self.base_instructions, runtime)

    @property
    def retry_nudge(self) -> str:
        return (f"\n\nYour previous reply could not be parsed. Reply with EXACTLY "
                f"one line:  SCORE: N/{self.cfg.max_score} - <short reason>")


# --- generate ------------------------------------------------------------------------------------
def frame_question(rt: Runtime, task: str) -> str:
    """Skill-informed QUERY FORMULATION before retrieval: bind the user's words to the dimensions/values
    the pack's skills describe so the tool doesn't guess. FAIL-SAFE: any error, an empty reply, or a
    rewrite that DRIFTS falls back to the raw task -- framing can only ADD precision, never CREATE an
    escalation."""
    skills = rt.frame_skills
    catalog = rt.retriever.catalog_block()             # opt-in: real low-cardinality values (authoritative)
    if catalog:
        skills = f"{skills}\n\n{catalog}"
    try:
        framed = rt.llm.complete(_fill(rt.prompt("frame.md"), task=task, skills=skills)).strip()
    except (RuntimeError, EmptyResponseError, ValueError, KeyError) as e:
        rt.log(f"  [frame] failed: {e}; using original question")
        return task
    if not framed or rewrite_drift(task, framed):
        rt.log("  [frame] discarded (empty or drifted); using original question")
        return task
    if framed != task:
        rt.log(f"  [frame] {task}  ->  {framed}")
    return framed


def _reformulate_loop(rt: Runtime, task: str, r: Retrieval, run_id: str) -> tuple[Retrieval, int, str]:
    """A BLANK retrieval is not proof the data is missing (a misread question looks identical). Give the
    strategy a bounded chance to self-correct: rephrase from the tool's hint and re-retrieve. The rewrite
    is NOT trusted blindly -- a refusal or a drift (subject substituted / identifier dropped) is final and
    decides the escalation REASON. Returns (retrieval, retries_spent, reason)."""
    retries, reason = 0, "no_data"
    while rt.retriever.reformulates and r.empty and retries < rt.cfg.max_data_retries:
        retries += 1
        hint = r.hint
        off_scope = hint or "The question asks about a subject this data does not cover."
        try:
            # the hint is UNTRUSTED tool output: bound it so it can't dominate the prompt
            new_q = rt.llm.complete(_fill(rt.prompt("reformulate.md"), task=task, feedback=hint[:600],
                                          known_values=rt.retriever.recovery_block())).strip()
        except (RuntimeError, EmptyResponseError, ValueError, KeyError) as e:
            rt.log(f"  [retry {retries}] failed: {e}; keeping no-data")
            r = Retrieval.blank(hint)
            continue
        if not new_q or new_q.upper().lstrip("*_# ").startswith(_REFUSE):
            rt.log(f"  [retry {retries}] reformulation REFUSED: subject not in this data")
            return Retrieval.blank(off_scope), retries, "out_of_scope"
        drift = rewrite_drift(task, new_q)
        if drift:
            rt.log(f"  [retry {retries}] reformulation DRIFTED ({drift}) -> {new_q}")
            hint_text = off_scope if drift == "out_of_scope" else (hint or "No rows matched the identifier in the question.")
            return Retrieval.blank(hint_text), retries, drift
        rt.log(f"  [retry {retries}] reformulated -> {new_q}")
        try:
            r = rt.retriever.retrieve(new_q, run_id)
        except (RuntimeError, ValueError, KeyError) as e:
            rt.log(f"  [retry {retries}] re-retrieval failed: {e}; keeping no-data")
            r = Retrieval.blank(hint)
    return r, retries, reason


def generate(rt: Runtime, s: dict) -> dict:
    run_id = s.get("run_id", "-")
    q = frame_question(rt, s["task"]) if (rt.cfg.frame_query and rt.retriever.frames_question) else s["task"]
    r = rt.retriever.retrieve(q, run_id)             # FIRST retrieval is unguarded: a hard error is fatal
    r, retries, reason = _reformulate_loop(rt, s["task"], r, run_id)
    grounded = not r.empty
    answer = rt.llm.complete(_fill(rt.prompt("generate.md"), instructions=rt.instructions_for(s),
                                   task=s["task"], data=r.text, observations=r.text,
                                   tools=rt.tools_desc, skills=rt.skills,
                                   exemplars=rt.exemplars, revision=0))
    rt.log(f"  [generate] rev0 (grounded={grounded}, retries={retries}) -> {answer}")
    if not grounded:
        # ESCALATE: keep the honest answer for the human; it is NOT scored (best_score stays -1, the
        # judge never runs) and the surfaces report the REASON: no_data vs out_of_scope.
        return {**s, "data": r.text, "answer": answer, "best_answer": answer, "iterations": 0,
                "grounded": False, "status": reason, "data_retries": retries}
    return {**s, "data": r.text, "answer": answer, "iterations": 0,
            "grounded": True, "status": "", "data_retries": retries}


# --- evaluate ------------------------------------------------------------------------------------
def _verification(rt: Runtime, s: dict) -> str:
    """Opt-in judge spot-check (`judge_verify_tool`): run ONE declared read-only tool against the task
    and hand its output to the rubric as independent evidence. Best-effort, never breaks scoring."""
    lt = rt.verify_tool
    res = dispatch(lt.tool, _render(lt.input_template, s["task"]),
                   ToolContext(run_id=s.get("run_id", "-"), timeout_s=rt.tool_timeout_s))
    return res.output if res.ok and (res.output or "").strip() else "(verification tool returned nothing)"


def evaluate(rt: Runtime, s: dict) -> dict:
    max_score, retries = rt.cfg.max_score, rt.cfg.eval_retries
    ok, _, err = validate_output(s["answer"], rt.cfg.output_schema)
    if not ok:
        # a declared output shape is a HARD contract: score 0 with the reason so refine fixes the shape
        verdict = Verdict(score=0, reason=f"OUTPUT SCHEMA VIOLATION: {err}")
        rt.log(f"  [evaluate] {verdict.reason}")
    else:
        evidence = s.get("data", "")
        if rt.verify_tool is not None:
            evidence += "\n\nVERIFICATION (independent read-only spot-check):\n" + _verification(rt, s)
        base = _fill(rt.prompt("rubric.md"), instructions=rt.instructions_for(s),
                     task=s["task"], answer=s["answer"], data=evidence, observations=evidence,
                     max_score=max_score)
        verdict = None
        for attempt in range(retries + 1):
            prompt = base if attempt == 0 else base + rt.retry_nudge
            try:
                raw = rt.eval_llm.complete(prompt)
            except EmptyResponseError as e:          # only a blank judge reply is retried; other errors propagate
                rt.log(f"  [evaluate] empty judge reply (attempt {attempt + 1}/{retries + 1}): {e}")
                continue
            try:
                verdict = parse_verdict(raw, max_score)
                break
            except (ValueError, ValidationError) as e:
                rt.log(f"  [evaluate] unusable verdict (attempt {attempt + 1}/{retries + 1}): {e}")
        if verdict is None:                          # retries exhausted -> fail closed
            verdict = Verdict(score=0, reason="judge output unparseable; scored 0 to force a refine")
    score = verdict.score
    feedback = f"SCORE: {score}/{max_score} - {verdict.reason}"
    rt.log(f"  [evaluate] score {score}/{max_score}  ({verdict.reason})")
    if score > s.get("best_score", -1):              # keep the champion AND the critique of it
        best_a, best_s, best_f, stall = s["answer"], score, feedback, 0
    else:
        best_a, best_s, best_f = s["best_answer"], s["best_score"], s.get("best_feedback", "")
        stall = s.get("stall", 0) + 1
    return {**s, "score": score, "feedback": feedback, "best_answer": best_a,
            "best_score": best_s, "best_feedback": best_f, "stall": stall}


# --- refine --------------------------------------------------------------------------------------
def refine(rt: Runtime, s: dict) -> dict:
    nxt = s["iterations"] + 1
    base_answer = s.get("best_answer") or s["answer"]          # refine from the BEST, not the latest
    base_feedback = s.get("best_feedback") or s["feedback"]
    try:
        answer = rt.llm.complete(_fill(rt.prompt("refine.md"), instructions=rt.instructions_for(s),
                                       task=s["task"], answer=base_answer, data=s.get("data", ""),
                                       observations=s.get("data", ""), feedback=base_feedback,
                                       skills=rt.skills, revision=nxt))
    except (RuntimeError, EmptyResponseError) as e:
        # a failed REFINEMENT is non-fatal: keep the already-scored best and end the loop
        rt.log(f"  [refine]   rev{nxt} worker failed: {e}; keeping best so far")
        return {**s, "refine_failed": True}
    rt.log(f"  [refine]   rev{nxt} -> {answer}")
    return {**s, "answer": answer, "iterations": nxt, "refine_failed": False}


# --- routers -------------------------------------------------------------------------------------
def keep_going(rt: Runtime, s: dict) -> str:
    if s["score"] >= rt.cfg.pass_score or s["iterations"] >= rt.cfg.max_iters:
        return "stop"
    if rt.cfg.max_stall and s.get("stall", 0) >= rt.cfg.max_stall:
        return "stop"
    return "refine"


def after_generate(s: dict) -> str:
    return "escalate" if not s.get("grounded", True) else "evaluate"


def after_refine(s: dict) -> str:
    return "stop" if s.get("refine_failed") else "evaluate"
