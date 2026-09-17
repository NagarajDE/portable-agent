"""
ENGINE / tools / planned -- OPT-IN, bounded MULTI-STEP reasoning over a VALIDATED DAG.

The model commits a PLAN (a directed acyclic graph of read-only tool steps) UP FRONT; the engine
validates it deterministically, executes it (resolving {{sN}} references between steps), and REPLANS
only on a step failure. When execution ends, the accumulated, labeled observations are returned as the
{data}/{observations} the existing `generate` node reasons over -- so scoring, grounding, refine, and
tracing are all inherited unchanged. Contrast:
  * deterministic -- engine runs ALL declared tools once (no model tool-choice).
  * agentic       -- model picks the NEXT tool from observations (free ReAct; adapts on success).
  * planned (HERE)-- model commits a validated DAG; engine executes; replans on FAILURE only.

Why a validated DAG (not free ReAct): a plan is auditable and deterministically checkable BEFORE any
tool runs -- the allowlist, the dependency graph, and the reference rules are enforced by `parse_plan`,
so a hallucinated or hostile plan cannot call an undeclared tool, form a cycle, or reference a step it
didn't declare. Every step still runs through the SAME `dispatch()` trust boundary (validation,
approval gate, timeout, redaction, bounds) as everything else, and writes are never auto-run.

Design choices (minimal + safe):
  * PORTABLE -- the plan is a JSON object in TEXT; no change to the LLMClient Protocol, so it works on
    any provider (a real model must emit the plan -- the MOCK provider can't, exactly like agentic).
  * READ-ONLY -- ctx.approved is False; a non-read-only step tool is refused, not run.
  * BOUNDED -- at most `max_plan_steps` dispatches and `max_replans` revisions per run.
  * GROUNDED -- a step whose result is the NO_DATA sentinel (or blank) is `empty`, NOT evidence; if NO
    step yields evidence the whole run returns the NO_DATA sentinel, so the loop escalates (never scores
    an answer written from nothing) -- the same contract as the single-SQL / deterministic paths.
  * REFERENCE SAFETY -- {{sN}} is replaced with a BOUNDED, single-line, sanitized SUMMARY of step sN's
    output (compact facts), single-pass (a summary containing {{sM}} is never re-expanded). The plan
    prompt frames all results as DATA, never instructions.
"""
from __future__ import annotations

import os
import re
import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from engine.sql_tool import no_data, is_no_data          # shared blank sentinel (stdlib-only: no cycle)
from engine.tools.base import ToolContext
from engine.tools.dispatch import dispatch
from engine.tools.orchestrator import describe_tools_schema
from engine.tracing import trace_event

_STEP_ID = re.compile(r"^s[1-9][0-9]*$")
_REF = re.compile(r"\{\{(s[1-9][0-9]*)\}\}")            # a reference to step sN's result
# Bound on a {{sN}} hand-off summary. Big enough to carry a list of ids (a top-N table), unlike the
# 600-char reformulate hint -- a small bound silently DROPPED the tail of a material list mid-chain (F1).
# Still bounded (and configurable via `max_ref_chars`) so an untrusted result can't dominate a prompt;
# truncation, when it does happen, is now VISIBLE (see _summarize).
_SUMMARY_MAX = 2000

# Plan-step field names a planner sometimes nests INSIDE `input` by mistake; stripped before dispatch
# (F4) so a common malformed plan doesn't fail an otherwise-fine step. Safety is unchanged -- dispatch
# still validates against the tool's input_model.
_RESERVED_INPUT_KEYS = {"id", "intent", "tool", "dependencies"}


# --- plan data structures (module-local; NEVER placed in graph State) ----------------------------
class PlanStep(BaseModel):
    model_config = ConfigDict(extra="forbid")          # a step may not smuggle unknown fields
    id: str
    intent: str
    tool: str
    input: dict[str, Any] = {}
    dependencies: list[str] = []


class Plan(BaseModel):
    model_config = ConfigDict(extra="ignore")          # tolerate a stray top-level "notes" key
    steps: list[PlanStep]


class StepResult(BaseModel):
    step_id: str
    status: Literal["success", "empty", "failed"]
    intent: str = ""
    output: str = ""
    error: str | None = None


class PlanError(ValueError):
    """A plan that failed deterministic validation, carrying a human-readable diagnostic."""


# --- plan extraction + validation (deterministic; mock-testable) ---------------------------------
def _extract_json_object(text: str) -> dict | None:
    """Parse the FIRST balanced {...} object in text (string/escape aware), or None. Tolerates a
    ```json fence and surrounding prose because the scan starts at the first '{' and stops at its
    match -- self-contained, no import from engine.graph."""
    if not text:
        return None
    start = text.find("{")
    if start == -1:
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                import json
                try:
                    obj = json.loads(text[start:i + 1])
                except (ValueError, TypeError):
                    return None
                return obj if isinstance(obj, dict) else None
    return None


def _refs_in(step: PlanStep) -> set[str]:
    import json
    return set(_REF.findall(json.dumps(step.input, default=str)))


def _check_acyclic(steps: list[PlanStep]) -> None:
    ids = {s.id for s in steps}
    pending = {s.id: {d for d in s.dependencies if d in ids} for s in steps}
    resolved: set[str] = set()
    changed = True
    while changed:
        changed = False
        for sid, deps in pending.items():
            if sid not in resolved and deps <= resolved:
                resolved.add(sid)
                changed = True
    if resolved != ids:
        raise PlanError(f"plan has a dependency cycle among {sorted(ids - resolved)}")


def parse_plan(text: str, allowed_tools: set[str], max_plan_steps: int,
               known_ids: set[str] = frozenset()) -> Plan:
    """Validate a planner reply into a Plan, or raise PlanError. This is the core security control:
    nothing executes until the plan is proven well-formed and allowlist-clean. `known_ids` are step ids
    that already SUCCEEDED on a prior pass (frozen); a REPLAN legitimately depends on / references them
    without re-listing them, so they count as valid dependency targets (BUG-1). Empty for the first plan."""
    obj = _extract_json_object(text)
    if obj is None:
        raise PlanError("planner did not return a JSON object")
    try:
        plan = Plan.model_validate(obj)
    except ValidationError as e:
        raise PlanError(f"plan does not match the schema: {e}") from e
    steps = plan.steps
    if not steps:
        raise PlanError("plan has no steps")
    if len(steps) > max_plan_steps:
        raise PlanError(f"plan has {len(steps)} steps > max_plan_steps ({max_plan_steps})")
    ids = [s.id for s in steps]
    if len(set(ids)) != len(ids):
        raise PlanError("plan has duplicate step ids")
    idset = set(ids)
    valid_deps = idset | set(known_ids)                # frozen successes from a prior pass are valid targets
    for s in steps:
        if not _STEP_ID.match(s.id):
            raise PlanError(f"bad step id {s.id!r} (must match ^s[1-9][0-9]*$)")
        if s.tool not in allowed_tools:
            raise PlanError(f"step {s.id} uses tool {s.tool!r} not in the allowlist "
                            f"{sorted(allowed_tools)}")
        if s.id in s.dependencies:
            raise PlanError(f"step {s.id} depends on itself")
        for d in s.dependencies:
            if d not in valid_deps:
                raise PlanError(f"step {s.id} depends on unknown step {d!r}")
        undeclared = _refs_in(s) - set(s.dependencies)
        if undeclared:
            raise PlanError(f"step {s.id} references {sorted(undeclared)} not listed in its "
                            f"dependencies")
    _check_acyclic(steps)
    return plan


# --- reference substitution (bounded, single-line, sanitized, single-pass) -----------------------
def _summarize(output: Any, max_chars: int = _SUMMARY_MAX) -> str:
    """A compact, single-line, printable summary of a step's output -- what {{sN}} substitutes.
    Bounded so an untrusted/runaway result can't dominate a downstream prompt; when the bound bites the
    truncation is VISIBLE (never silent, F1) so the next step / the human knows the hand-off was cut."""
    s = " ".join(str(output).split())                  # collapse all whitespace/newlines
    s = "".join(ch for ch in s if ch.isprintable())    # strip control chars
    if len(s) <= max_chars:
        return s
    return s[:max_chars] + f" …(+{len(s) - max_chars} chars truncated — narrow this step)"


def _clean_input(inp: Any) -> dict:
    """Drop reserved plan-step keys a planner sometimes nests inside `input` (F4). Top-level only;
    dispatch still validates the result, so this only rescues a common malformed plan, never widens it."""
    if not isinstance(inp, dict):
        return {}
    return {k: v for k, v in inp.items() if k not in _RESERVED_INPUT_KEYS}


def _resolve_refs(value: Any, summaries: dict[str, str]) -> Any:
    """Replace every {{sN}} in string values with sN's summary, recursing dicts/lists. SINGLE-PASS:
    re.sub does not re-scan replacement text, so a summary that itself contains {{sM}} is left literal
    (no recursion)."""
    if isinstance(value, str):
        return _REF.sub(lambda m: summaries.get(m.group(1), m.group(0)), value)
    if isinstance(value, dict):
        return {k: _resolve_refs(v, summaries) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_refs(v, summaries) for v in value]
    return value


# --- planning (with a bounded repair retry for the INITIAL plan) ---------------------------------
def _render_plan_prompt(tmpl, task, catalog, skills, max_steps, results="", failed="", repair=""):
    from engine.graph import fill                       # lazy: avoid graph<->tools import cycle
    return fill(tmpl, task=task, tools=catalog, skills=skills or "None.",
                max_steps=str(max_steps), results=results, failed=failed, repair=repair)


_REPAIR = ("\n\nYOUR PREVIOUS PLAN WAS INVALID: {diag}\nReturn ONE corrected JSON object of the form "
           '{{"steps": [ ... ]}} and nothing else.')

# The planner's OUTPUT budget. A DAG is one large structured generation (every step's intent + input +
# deps, plus any preamble a model adds), and live planned runs hit the general LLM_MAX_TOKENS cap and
# failed. So the plan call asks for its OWN cap: PLAN_MAX_TOKENS (default 8192), never below the general
# cap. A cap is a ceiling, not a spend -- a short plan costs the same tokens either way. (The SYNTHESIS
# call that follows has its own ceiling: the general default, or the pack's `max_output_tokens`.)
_PLAN_MAX_TOKENS_DEFAULT = 8192


def _plan_budget() -> int:
    from engine.llm_client import DEFAULT_MAX_TOKENS          # one source for the general default
    try:
        general = int(os.getenv("LLM_MAX_TOKENS", str(DEFAULT_MAX_TOKENS)))
        plan = int(os.getenv("PLAN_MAX_TOKENS", str(_PLAN_MAX_TOKENS_DEFAULT)))
    except ValueError:
        raise ValueError("LLM_MAX_TOKENS / PLAN_MAX_TOKENS must be positive integers")
    return max(1, general, plan)


def _ask_once(llm, tmpl, allowed, max_steps, render, repair="",
              known_ids=frozenset()) -> tuple[Plan | None, str]:
    budget = _plan_budget()
    try:
        raw = llm.complete(_render_plan_prompt(tmpl, max_steps=max_steps, repair=repair, **render),
                           max_tokens=budget)
    except Exception as e:                              # a planner call failure is not fatal here
        if "truncated" in str(e).lower():               # make the fix ACTIONABLE: name the planner's own knob
            return None, (f"planner output truncated at {budget} tokens; raise PLAN_MAX_TOKENS, trim "
                          f"plan_skills, or lower max_plan_steps")
        return None, f"planner call failed: {e}"
    try:
        return parse_plan(raw, allowed, max_steps, known_ids), ""
    except PlanError as e:
        return None, str(e)


def _plan_with_repair(llm, tmpl, allowed, max_steps, render,
                      known_ids=frozenset()) -> tuple[Plan | None, str]:
    """Ask for a plan; on an invalid reply, give ONE bounded repair retry. Used for BOTH the initial
    plan and each replan (F3), so a replan gets the same second chance the first plan already had.
    `known_ids` (frozen successes) are passed through so a replan may depend on them (BUG-1)."""
    plan, diag = _ask_once(llm, tmpl, allowed, max_steps, render, known_ids=known_ids)
    if plan is not None:
        return plan, ""
    return _ask_once(llm, tmpl, allowed, max_steps, render, repair=_REPAIR.format(diag=diag),
                     known_ids=known_ids)


# --- execution -----------------------------------------------------------------------------------
def _next_ready(plan: Plan, results: dict[str, StepResult]) -> PlanStep | None:
    for st in plan.steps:
        if st.id in results:
            continue
        if all(results.get(d) is not None and results[d].status == "success"
               for d in st.dependencies):
            return st
    return None


def _observations(results: dict[str, StepResult]) -> str:
    """Labeled observations from every recorded step (in execution order); NO_DATA sentinel if NOT
    ONE step produced usable evidence -- so the loop escalates instead of scoring an empty answer."""
    blocks, successes = [], 0
    for sr in results.values():
        label = f"[{sr.step_id} {sr.intent}]".rstrip()
        if sr.status == "success":
            blocks.append(f"{label}\n{sr.output}")
            successes += 1
        elif sr.status == "empty":
            blocks.append(f"{label} (no data)")
        else:
            blocks.append(f"{label} (unavailable: {sr.error})")
    text = "\n\n".join(blocks)
    return text if successes else no_data(text or "No step produced usable evidence.")


def run_planned(task: str, loaded, llm, plan_tmpl: str, replan_tmpl: str, frame_skills: str = "",
                run_id: str = "-", use_case: str = "-",
                max_plan_steps: int = 8, max_replans: int = 2, max_ref_chars: int = _SUMMARY_MAX) -> str:
    """Plan -> execute (with {{sN}} chaining) -> replan-on-failure, bounded. Returns the accumulated
    labeled observations for `generate`, or the NO_DATA sentinel if nothing usable was gathered."""
    timeout = max(1.0, float(os.getenv("TOOL_TIMEOUT_SECONDS", "30")))
    ctx = ToolContext(run_id=run_id or "-", timeout_s=timeout, approved=False)   # never auto-run writes
    by_label = {lt.label: lt for lt in loaded}
    allowed = set(by_label)
    catalog = describe_tools_schema(loaded)
    render = dict(task=task, catalog=catalog, skills=frame_skills)

    plan, diag = _plan_with_repair(llm, plan_tmpl, allowed, max_plan_steps,
                                   {**render, "results": "", "failed": ""})
    if plan is None:
        trace_event(run_id, use_case, "plan_failed", reason=diag)
        return no_data(f"Could not form a valid multi-step plan ({diag}).")
    trace_event(run_id, use_case, "plan_created", steps=len(plan.steps))

    results: dict[str, StepResult] = {}                 # only SUCCESSFUL results survive a replan
    summaries: dict[str, str] = {}
    replans = dispatched = 0

    while True:
        step = _next_ready(plan, results)
        if step is None or dispatched >= max_plan_steps:
            break
        dispatched += 1
        resolved = _resolve_refs(_clean_input(step.input), summaries)   # strip nested reserved keys (F4)
        t0 = time.perf_counter()
        res = dispatch(by_label[step.tool].tool, resolved, ctx)
        ms = round((time.perf_counter() - t0) * 1000)
        if not res.ok:
            sr = StepResult(step_id=step.id, status="failed", intent=step.intent,
                            error=(res.error.kind if res.error else "error"))
        elif is_no_data(res.output) or not str(res.output or "").strip():
            sr = StepResult(step_id=step.id, status="empty", intent=step.intent,
                            output=str(res.output or ""))
        else:
            sr = StepResult(step_id=step.id, status="success", intent=step.intent,
                            output=str(res.output))
            summaries[step.id] = _summarize(res.output, max_ref_chars)     # bounded, VISIBLE truncation (F1)
        results[step.id] = sr
        trace_event(run_id, use_case, "plan_step", id=step.id, tool=step.tool, status=sr.status, ms=ms)

        if sr.status != "success" and replans < max_replans:
            replans += 1                               # count the ATTEMPT so replan churn is bounded
            frozen_ids = {r.step_id for r in results.values() if r.status == "success"}   # BUG-1
            frozen = "\n".join(f"- {r.step_id} ({r.intent}): success -> {_summarize(r.output, 200)}"
                               for r in results.values() if r.status == "success") or "None."
            # Tell the replanner WHY the step needs revising: an EMPTY step is usually a query-phrasing
            # gap (rephrase / split into single-measure steps), not missing data -- distinct from a hard
            # failure. This steers a more useful revision than a bare step id.
            failed_desc = (f"{step.id} — returned NO ROWS; try rephrasing it more simply or splitting "
                           f"it into single-measure steps (the data may still exist)"
                           if sr.status == "empty" else f"{step.id} — failed ({sr.error})")
            newplan, rdiag = _plan_with_repair(llm, replan_tmpl, allowed, max_plan_steps,
                                               {**render, "results": frozen, "failed": failed_desc},
                                               known_ids=frozen_ids)   # a replan may depend on frozen steps
            if newplan is not None:
                trace_event(run_id, use_case, "replan", n=replans)
                plan = newplan
                # freeze ONLY successes: a failed/empty id may be retried under the revised plan.
                results = {sid: r for sid, r in results.items() if r.status == "success"}
                summaries = {sid: summaries[sid] for sid in results if sid in summaries}
            else:                                      # F3: a failed replan is now visible, not silent
                trace_event(run_id, use_case, "replan_failed", n=replans, reason=rdiag)

    return _observations(results)
