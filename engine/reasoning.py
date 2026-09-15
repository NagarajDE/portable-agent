"""ENGINE / reasoning -- multi-step plan-and-execute reasoning subgraph.

Extends the portable-agent from single-query retrieval to MULTI-STEP data workflows where
step N's output parameterizes step N+1. Packs opt in via `reasoning.mode: multi_step` in
their config.yaml; single-query packs are byte-for-byte unchanged (zero overhead).

The subgraph has 4 nodes:
  planner        -> LLM produces a JSON plan (DAG of tool calls)
  step_executor  -> runs ONE ready step per invocation (self-loop)
  replan         -> revises plan on failure (successful steps immutable)
  synthesize     -> combines all evidence into a grounded answer

The synthesized answer feeds into the existing evaluate->refine quality loop.
Design doc: docs/design/multi-step-reasoning.md
"""
from __future__ import annotations

import json
import re
import os
from pathlib import Path
from typing import Literal
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field, field_validator

from engine.llm_client import LLMClient, EmptyResponseError


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

class PlanStep(BaseModel):
    """One step in a reasoning plan.  `query` may contain {{step_id}} references
    to prior step outputs -- resolved at execution time via single-pass substitution."""
    model_config = ConfigDict(extra="forbid")

    id: str
    intent: str
    tool: str
    query: str
    dependencies: list[str] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _valid_id(cls, v: str) -> str:
        if not re.fullmatch(r"s[1-9][0-9]*", v):
            raise ValueError(f"step id must match s1, s2, ...; got {v!r}")
        return v


class Plan(BaseModel):
    """A validated DAG of PlanSteps."""
    model_config = ConfigDict(extra="forbid")
    steps: list[PlanStep] = Field(min_length=1, max_length=20)


class StepResult(BaseModel):
    """The outcome of executing one plan step."""
    model_config = ConfigDict(extra="forbid")
    step_id: str
    status: Literal["success", "failed"]
    output: str = ""
    evidence: list[str] = Field(default_factory=list)
    error: str | None = None


# ---------------------------------------------------------------------------
# Plan validation
# ---------------------------------------------------------------------------

class PlanValidationError(ValueError):
    """Raised when a plan fails structural or security validation."""


def parse_plan(text: str, allowed_tools: set[str],
               max_steps: int = 20) -> Plan:
    """Parse and validate a JSON plan from LLM output.

    Validates: JSON syntax, schema conformance (extra=forbid), unique IDs,
    tool allowlist, declared dependencies, reference integrity, DAG acyclicity,
    and step count budget.  Raises PlanValidationError on any violation.
    """
    text = text.strip()
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)

    try:
        raw = json.loads(text)
    except json.JSONDecodeError as e:
        raise PlanValidationError(f"Invalid JSON: {e}") from e

    try:
        plan = Plan.model_validate(raw)
    except Exception as e:
        raise PlanValidationError(f"Schema error: {e}") from e

    if len(plan.steps) > max_steps:
        raise PlanValidationError(
            f"Plan has {len(plan.steps)} steps; max is {max_steps}")

    by_id = {step.id: step for step in plan.steps}
    if len(by_id) != len(plan.steps):
        raise PlanValidationError("Duplicate step IDs")

    for step in plan.steps:
        if step.tool not in allowed_tools:
            raise PlanValidationError(f"Disallowed tool: {step.tool!r}")
        if not set(step.dependencies) <= by_id.keys():
            unknown = set(step.dependencies) - by_id.keys()
            raise PlanValidationError(
                f"Step {step.id} has unknown dependencies: {unknown}")
        refs = set(re.findall(r"\{\{([^{}]+)\}\}", step.query))
        if not refs <= set(step.dependencies):
            undeclared = refs - set(step.dependencies)
            raise PlanValidationError(
                f"Step {step.id} references undeclared dependencies: {undeclared}")

    # DAG acyclicity check
    resolved: set[str] = set()
    while len(resolved) < len(by_id):
        ready = {
            sid for sid, step in by_id.items()
            if sid not in resolved and set(step.dependencies) <= resolved
        }
        if not ready:
            raise PlanValidationError("Dependency cycle detected")
        resolved |= ready

    return plan


# ---------------------------------------------------------------------------
# Execution helpers
# ---------------------------------------------------------------------------

def ready_steps(plan: Plan, results: dict[str, StepResult]) -> list[PlanStep]:
    """Return steps whose dependencies have ALL succeeded and that haven't run yet."""
    succeeded = {sid for sid, r in results.items() if r.status == "success"}
    return [
        step for step in plan.steps
        if step.id not in results and set(step.dependencies) <= succeeded
    ]


def next_phase(plan: Plan, results: dict[str, StepResult]) -> str:
    """Decide what the graph should do next."""
    if any(r.status == "failed" for r in results.values()):
        return "replan"
    if len(results) == len(plan.steps):
        return "synthesize"
    return "execute" if ready_steps(plan, results) else "replan"


def resolve_query(step: PlanStep, results: dict[str, StepResult]) -> str:
    """Substitute {{dep_id}} references with step outputs.
    SINGLE-PASS only -- nested {{}} in results stays literal (prevents injection)."""
    def _sub(match):
        dep_id = match.group(1)
        if dep_id not in step.dependencies:
            raise PlanValidationError(f"Undeclared reference {dep_id}")
        r = results.get(dep_id)
        if r is None or r.status != "success":
            raise PlanValidationError(f"Reference {dep_id} has no successful result")
        return r.output
    return re.sub(r"\{\{([^{}]+)\}\}", _sub, step.query)


# ---------------------------------------------------------------------------
# Prompt helpers
# ---------------------------------------------------------------------------

SHARED = Path(__file__).resolve().parent.parent / "shared"


def _load_prompt(use_case: str, name: str) -> str:
    path = SHARED / "prompts" / name
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    raise FileNotFoundError(f"Prompt not found: {path}")


def _fill(template: str, **kwargs) -> str:
    for k, v in kwargs.items():
        template = template.replace(f"{{{k}}}", str(v))
    return template


# ---------------------------------------------------------------------------
# Platform limits (not overridable by packs)
# ---------------------------------------------------------------------------

PLATFORM_MAX_REPLANS = 3
PLATFORM_MAX_STEPS = 30
PLATFORM_MAX_PLAN_STEPS = 20


# ---------------------------------------------------------------------------
# Build reasoning nodes as closures
# ---------------------------------------------------------------------------

def build_reasoning_nodes(
    use_case: str,
    llm: LLMClient,
    tools: dict,
    allowed_tools: set[str],
    *,
    planning_skills: str = "None.",
    synthesis_skills: str = "None.",
    max_replans: int = 2,
    max_executed_steps: int = 12,
    max_plan_steps: int = 12,
    verbose: bool = True,
):
    """Build the 4 reasoning node functions as closures over configuration.
    Returns dict: {planner, step_executor, replan, synthesize}."""
    max_replans = min(max_replans, PLATFORM_MAX_REPLANS)
    max_executed_steps = min(max_executed_steps, PLATFORM_MAX_STEPS)
    max_plan_steps = min(max_plan_steps, PLATFORM_MAX_PLAN_STEPS)

    def log(msg: str):
        if verbose:
            print(msg)

    # Load prompt templates
    try:
        planner_tmpl = _load_prompt(use_case, "planner.md")
    except FileNotFoundError:
        planner_tmpl = ("Produce a JSON plan to answer this question.\n"
                        "Allowed tools: {allowed_tools}\nSkills:\n{skills}\n\n"
                        "Question: {task}\n\nReturn ONLY valid JSON.")
    try:
        replan_tmpl = _load_prompt(use_case, "replan.md")
    except FileNotFoundError:
        replan_tmpl = ("Revise the plan. Keep successful steps unchanged.\n"
                       "Previous results:\n{previous_results}\n"
                       "Diagnostic: {diagnostic}\nAllowed tools: {allowed_tools}\n"
                       "Question: {task}\n\nReturn ONLY valid JSON.")
    try:
        synth_tmpl = _load_prompt(use_case, "synthesize_plan.md")
    except FileNotFoundError:
        synth_tmpl = ("Answer the question using ONLY evidence from executed steps.\n"
                      "Cite step IDs [s1] for factual claims. Disclose failures and gaps.\n\n"
                      "Skills:\n{skills}\n\nQuestion: {task}\n\n"
                      "Evidence:\n{evidence}\n\nDiagnostic: {diagnostic}")

    def _request_plan(task, previous=None, results=None, diagnostic=""):
        results = results or {}
        tmpl = replan_tmpl if previous else planner_tmpl
        prev_json = ""
        if previous:
            prev_json = json.dumps({
                "previous_plan": [s.model_dump() for s in previous.steps],
                "results": {sid: r.model_dump() for sid, r in results.items()},
            }, indent=2)

        prompt = _fill(tmpl, task=task,
                       allowed_tools=", ".join(sorted(allowed_tools)),
                       skills=planning_skills, previous_results=prev_json,
                       diagnostic=diagnostic)

        for attempt in range(2):
            try:
                text = llm.complete(prompt)
                plan = parse_plan(text, allowed_tools, max_plan_steps)
                if previous:
                    frozen = {s.id: s for s in previous.steps
                              if s.id in results and results[s.id].status == "success"}
                    proposed = {s.id: s for s in plan.steps}
                    for sid, orig in frozen.items():
                        if sid not in proposed:
                            raise PlanValidationError(f"Replan dropped successful step {sid}")
                        if proposed[sid] != orig:
                            raise PlanValidationError(f"Replan modified successful step {sid}")
                return plan
            except (PlanValidationError, json.JSONDecodeError) as e:
                if attempt == 0:
                    prompt += f"\n\nInvalid: {e}\nReturn corrected JSON only."
                else:
                    raise PlanValidationError(f"Plan failed after retry: {e}") from e
        raise PlanValidationError("unreachable")

    # --- Node: planner ---
    def planner(s: dict) -> dict:
        log(f"  [planner] decomposing: {s['task'][:80]}")
        init = {"results": {}, "active_step_id": None,
                "replan_count": 0, "executed_count": 0, "diagnostic": ""}
        try:
            plan = _request_plan(s["task"])
            log(f"  [planner] {len(plan.steps)} steps: {[st.id for st in plan.steps]}")
            return {**init, "plan": plan.model_dump(), "phase": "execute"}
        except (PlanValidationError, RuntimeError, EmptyResponseError) as e:
            log(f"  [planner] failed: {e}")
            return {**init, "plan": {"steps": []}, "phase": "synthesize",
                    "diagnostic": str(e)}

    # --- Node: step_executor ---
    def step_executor(s: dict) -> dict:
        plan = Plan.model_validate(s["plan"])
        results = {sid: StepResult.model_validate(r)
                   for sid, r in s.get("results", {}).items()}
        executed = s.get("executed_count", 0)

        if executed >= max_executed_steps:
            log(f"  [executor] budget exhausted ({executed}/{max_executed_steps})")
            return {"phase": "synthesize", "active_step_id": None,
                    "diagnostic": "Execution budget exhausted"}

        rdy = ready_steps(plan, results)
        if not rdy:
            phase = next_phase(plan, results)
            log(f"  [executor] no ready steps -> {phase}")
            return {"phase": phase, "active_step_id": None}

        step = rdy[0]
        log(f"  [executor] running {step.id}: {step.intent[:60]}")

        try:
            query = resolve_query(step, results)
            fn = tools.get(step.tool)
            if fn is None:
                raise RuntimeError(f"Tool {step.tool!r} not in registry")
            raw = fn(query)
            if isinstance(raw, tuple):
                output, evidence = str(raw[0]), list(raw[1]) if len(raw) > 1 else []
            else:
                output, evidence = str(raw), []
            result = StepResult(step_id=step.id, status="success",
                                output=output[:65536], evidence=evidence)
            log(f"  [executor] {step.id} ok ({len(output)} chars)")
        except Exception as exc:
            result = StepResult(step_id=step.id, status="failed",
                                output="", error=f"{type(exc).__name__}: {exc}")
            log(f"  [executor] {step.id} failed: {exc}")

        new_results = {**{sid: r.model_dump() for sid, r in results.items()},
                       step.id: result.model_dump()}
        parsed = {sid: StepResult.model_validate(r) for sid, r in new_results.items()}
        phase = next_phase(plan, parsed)
        new_count = executed + 1
        if new_count >= max_executed_steps and phase != "synthesize":
            phase = "synthesize"

        return {"results": new_results, "active_step_id": step.id,
                "executed_count": new_count, "phase": phase}

    # --- Node: replan ---
    def replan(s: dict) -> dict:
        count = s.get("replan_count", 0)
        if count >= max_replans:
            log(f"  [replan] budget exhausted ({count}/{max_replans})")
            return {"phase": "synthesize", "active_step_id": None,
                    "diagnostic": "Replanning budget exhausted",
                    "replan_count": count}

        plan = Plan.model_validate(s["plan"])
        results = {sid: StepResult.model_validate(r)
                   for sid, r in s.get("results", {}).items()}
        count += 1
        try:
            new_plan = _request_plan(s["task"], plan, results, s.get("diagnostic", ""))
            kept = {sid: r.model_dump() for sid, r in results.items()
                    if r.status == "success"}
            kept_parsed = {sid: StepResult.model_validate(r) for sid, r in kept.items()}
            log(f"  [replan] rev {count}: {len(new_plan.steps)} steps, kept {len(kept)}")
            return {"plan": new_plan.model_dump(), "results": kept,
                    "replan_count": count, "active_step_id": None,
                    "phase": next_phase(new_plan, kept_parsed), "diagnostic": ""}
        except (PlanValidationError, RuntimeError, EmptyResponseError) as e:
            log(f"  [replan] failed: {e}")
            return {"replan_count": count, "phase": "synthesize",
                    "active_step_id": None, "diagnostic": str(e)}

    # --- Node: synthesize ---
    def synthesize(s: dict) -> dict:
        results = {sid: StepResult.model_validate(r)
                   for sid, r in s.get("results", {}).items()}
        diagnostic = s.get("diagnostic", "")
        plan_steps = Plan.model_validate(s["plan"]).steps if s.get("plan", {}).get("steps") else []

        evidence_lines = []
        for step in plan_steps:
            r = results.get(step.id)
            if r and r.status == "success":
                evidence_lines.append(f"[{step.id}] {step.intent}\n{r.output[:4000]}")
            elif r and r.status == "failed":
                evidence_lines.append(f"[{step.id}] {step.intent} -- FAILED: {r.error or 'unknown'}")

        evidence = "\n\n".join(evidence_lines) if evidence_lines else "No evidence gathered."
        prompt = _fill(synth_tmpl, task=s["task"], skills=synthesis_skills,
                       evidence=evidence, diagnostic=diagnostic)

        try:
            answer = llm.complete(prompt)
        except (RuntimeError, EmptyResponseError) as e:
            ok_count = sum(1 for r in results.values() if r.status == "success")
            answer = (f"Unable to fully answer. {ok_count} steps succeeded but "
                      f"synthesis failed: {e}")

        log(f"  [synthesize] answer ({len(answer)} chars) from {len(evidence_lines)} steps")
        has_evidence = any(r.status == "success" for r in results.values())
        return {"answer": answer, "best_answer": answer, "data": evidence,
                "phase": "done", "grounded": has_evidence,
                "status": "" if has_evidence else "no_data"}

    return {"planner": planner, "step_executor": step_executor,
            "replan": replan, "synthesize": synthesize}


# ---------------------------------------------------------------------------
# Build the reasoning subgraph (LangGraph)
# ---------------------------------------------------------------------------

def build_reasoning_graph(
    use_case: str, llm: LLMClient, tools: dict, allowed_tools: set[str], *,
    planning_skills: str = "None.", synthesis_skills: str = "None.",
    max_replans: int = 2, max_executed_steps: int = 12,
    max_plan_steps: int = 12, verbose: bool = True,
):
    """Build and compile the 4-node reasoning subgraph."""
    from langgraph.graph import StateGraph, END
    from typing import TypedDict

    nodes = build_reasoning_nodes(
        use_case, llm, tools, allowed_tools,
        planning_skills=planning_skills, synthesis_skills=synthesis_skills,
        max_replans=max_replans, max_executed_steps=max_executed_steps,
        max_plan_steps=max_plan_steps, verbose=verbose)

    class ReasoningState(TypedDict, total=False):
        task: str
        answer: str
        best_answer: str
        data: str
        grounded: bool
        status: str
        run_id: str
        instructions: str
        plan: dict
        results: dict
        active_step_id: str | None
        phase: str
        replan_count: int
        executed_count: int
        diagnostic: str

    g = StateGraph(ReasoningState)
    g.add_node("planner", nodes["planner"])
    g.add_node("step_executor", nodes["step_executor"])
    g.add_node("replan", nodes["replan"])
    g.add_node("synthesize", nodes["synthesize"])
    g.set_entry_point("planner")

    routes = {"execute": "step_executor", "replan": "replan",
              "synthesize": "synthesize"}
    for src in ("planner", "step_executor", "replan"):
        g.add_conditional_edges(src, lambda s: s.get("phase", "synthesize"), routes)
    g.add_edge("synthesize", END)
    return g.compile()
