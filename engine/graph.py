"""
ENGINE / the loop.  GENERIC and platform-neutral.  This module is the COMPOSITION ROOT: it turns a
use-case pack name into a compiled LangGraph by (1) validating the pack config, (2) building the
dependencies (models, retriever strategy, prompts, skills), and (3) wiring the nodes. The nodes
themselves live in engine/nodes.py; reading a pack lives in engine/packs.py; scoring in
engine/verdict.py; the rewrite guards in engine/guards.py; the typed retrieval in engine/retrieval.py.

    loop: true   generate -> (escalate | evaluate) -> (refine -> evaluate ...) -> END
    loop: off    generate -> END          (the default: gathering + grounding, but no judge, unscored)

Never changes per use case or platform.
"""
from __future__ import annotations

import uuid
import warnings
from functools import partial
from typing import TypedDict

from langgraph.graph import StateGraph, END

from engine import packs as _p
from engine.config import PackConfig
from engine.llm_client import (get_llm_client, get_eval_client, get_planner_client, model_summary,
                               LLMClient)
from engine.nodes import Runtime, generate, evaluate, refine, keep_going, after_generate, after_refine
from engine.retrieval import (Retriever, SqlRetriever, DeterministicRetriever, AgenticRetriever,
                              PlannedRetriever, ToollessRetriever)
from engine.settings import settings as _settings
from engine.sql_tool import get_sql_tool, SQLTool
from engine.tools import load_tools, describe_tools
from engine.tracing import instrument, guard_third_party_telemetry   # observability applied from OUTSIDE

# Pack roots are bound HERE (the composition root) and passed explicitly to every loader, so a test can
# point the whole engine at a temporary tree by patching these two names.
REPO_ROOT, USECASES, SHARED = _p.REPO_ROOT, _p.USECASES, _p.SHARED

# --- re-exports (the public reading/scoring API; implemented in their own modules) -------------------
from engine.verdict import Verdict, parse_verdict, _coerce_score, _first_json_object      # noqa: E402,F401
from engine.guards import (_keeps_subject, _keeps_pinned, _content_words, _measure_words,   # noqa: E402,F401
                           _pinned_codes, _stem, _GENERIC, _REFUSE, rewrite_drift)
from engine.packs import _merge_models, _instr_block, _format_exemplar, fill, _fill      # noqa: E402,F401


def load_config(use_case: str) -> dict:
    return _p.load_config(use_case, usecases=USECASES, shared=SHARED)


def load_semantic_layer(use_case: str) -> dict | None:
    return _p.load_semantic_layer(use_case, usecases=USECASES)


def _golden_questions(use_case: str) -> list[str]:
    return _p._golden_questions(use_case, usecases=USECASES)


def pack_manifest(use_case: str) -> dict:
    """Composed HERE (not delegated) so it goes through this module's patchable `load_config` /
    `_golden_questions` -- the seams tests and runners rely on."""
    cfg = load_config(use_case)
    samples = cfg.get("sample_questions") or _golden_questions(use_case)
    samples = [str(s).strip() for s in (samples or []) if str(s).strip()][:20]
    return {"use_case": use_case,
            "name": cfg.get("name", use_case),
            "description": str(cfg.get("description", "")).strip(),
            "sample_task": str(cfg.get("sample_task", "")).strip(),
            "sample_questions": samples}


def _prompt(use_case: str, name: str) -> str:
    return _p._prompt(use_case, name, usecases=USECASES, shared=SHARED)


def load_skills(use_case: str, exclude_shared: list | None = None) -> str:
    return _p.load_skills(use_case, exclude_shared, usecases=USECASES, shared=SHARED)


def load_named_skills(use_case: str, names) -> str:
    return _p.load_named_skills(use_case, names, usecases=USECASES, shared=SHARED)


def load_instructions(use_case: str, exclude: list | None = None) -> str:
    return _p.load_instructions(use_case, exclude, usecases=USECASES, shared=SHARED)


def load_exemplars(use_case: str) -> str:
    return _p.load_exemplars(use_case, usecases=USECASES)


def load_glossary(use_case: str) -> list[dict]:
    return _p.load_glossary(use_case, usecases=USECASES, shared=SHARED)


# --- state -----------------------------------------------------------------------------------------
class State(TypedDict):
    task: str
    data: str
    answer: str
    feedback: str
    score: int
    best_answer: str
    best_score: int
    best_feedback: str        # the critique that produced best_answer (refine builds from BEST)
    stall: int                # consecutive refines that did NOT beat best_score (no-progress stop)
    iterations: int
    refine_failed: bool       # a refine worker call raised -> route straight to END
    grounded: bool            # did retrieval yield usable data? False -> escalate (skip the judge)
    status: str               # ""=normal; "no_data" | "out_of_scope" -> handed to a human, unscored
    data_retries: int         # reformulate-and-retry attempts spent
    instructions: str         # per-request OPERATOR instructions (applied only if the pack allows)
    run_id: str               # per-run id; the join key for observability
    thread_id: str            # session/conversation id seam (multi-turn is a future capability)


def initial_state(task: str, instructions: str = "", thread_id: str = "") -> State:
    return {"task": task, "data": "", "answer": "", "feedback": "",
            "score": -1, "best_answer": "", "best_score": -1, "best_feedback": "",
            "stall": 0, "iterations": 0, "refine_failed": False,
            "grounded": True, "status": "", "data_retries": 0,
            "instructions": instructions or "", "run_id": uuid.uuid4().hex[:12],
            "thread_id": thread_id or ""}


# --- composition root ------------------------------------------------------------------------------
def _build_retriever(cfg: PackConfig, use_case: str, llm, sql, loaded_tools, log, timeout_s,
                     plan_skills_text: str, planner=None) -> Retriever:
    z = cfg.zero_is_no_data
    if loaded_tools is None:                                   # legacy: no `tools:` key -> single SQL
        return SqlRetriever(sql, zero_is_no_data=z, recover_dims=cfg.recover_value_dims,
                            value_catalog=cfg.value_catalog, timeout_s=timeout_s, log=log)
    if cfg.tool_mode == "agentic":
        return AgenticRetriever(loaded_tools, llm, _prompt(use_case, "act.md"), cfg.max_tool_steps,
                                zero_is_no_data=z)
    if cfg.tool_mode == "planned":
        # the PLANNER may be a separate (faster) model (`models.planner` / PLANNER_*); default = the worker
        return PlannedRetriever(loaded_tools, planner or llm, _prompt(use_case, "plan.md"),
                                _prompt(use_case, "replan.md"), plan_skills_text, use_case,
                                cfg.max_plan_steps, cfg.max_replans, cfg.max_ref_chars, zero_is_no_data=z)
    if not loaded_tools:                                       # `tools: []` -> deliberately toolless
        return ToollessRetriever(z)
    return DeterministicRetriever(loaded_tools, zero_is_no_data=z)


def _resolve_loop(cfg: PackConfig, use_case: str, eval_llm, injected_llm, log) -> tuple[bool, LLMClient | None]:
    """R1: the loop runs ONLY when the pack says `loop: true` AND a judge can be built. A truthy flag whose
    evaluator cannot be constructed (a model-required provider with no model, an unknown provider, a
    missing SDK or credential) falls back to NON-LOOP with a WARNING -- never a failed build. An evaluator
    that merely inherits the worker's provider/model is configured (by inheritance), not missing."""
    if not cfg.loop:
        log("  [loop]     off (pack sets no `loop: true`): worker answer is final, unscored")
        return False, None
    if eval_llm is None:
        eval_llm = injected_llm                                # a test's worker double judges too
    if eval_llm is None:
        try:
            eval_llm = get_eval_client(use_case, cfg.models)
        except Exception as e:                                 # config/SDK/credential -- the reason is surfaced
            msg = (f"pack {use_case!r} sets loop: true but no evaluator could be built "
                   f"({type(e).__name__}: {e}); running NON-LOOP (unscored). Configure models.evaluator "
                   f"or EVAL_PROVIDER/EVAL_MODEL to restore scoring.")
            warnings.warn(msg, RuntimeWarning, stacklevel=3)
            log(f"  [loop]     WARNING: {msg}")
            return False, None
    return True, eval_llm


def build_graph(use_case: str, llm: LLMClient | None = None,
                sql: SQLTool | None = None, eval_llm: LLMClient | None = None,
                semantic_layer: dict | None = None, instructions: str | None = None,
                verbose: bool = True):
    """Compose a pack into a compiled graph. `llm`/`eval_llm`/`sql`/`semantic_layer`/`instructions`
    are dependency-injection seams (tests and runner scripts); unset -> resolved from config/env."""
    cfg = PackConfig.from_dict(load_config(use_case))          # typos and bad bounds fail HERE
    st = _settings()
    guard_third_party_telemetry()                              # LangSmith cloud tracing: off unless allowed

    def log(m):
        if verbose:
            print(m)

    injected_llm = llm
    llm = llm or get_llm_client(use_case, cfg.models)
    log(f"  [models]   {model_summary(cfg.models)}")
    loop, eval_llm = _resolve_loop(cfg, use_case, eval_llm, injected_llm, log)
    planner = None if injected_llm is not None else get_planner_client(use_case, cfg.models)   # None -> worker

    loaded_tools = load_tools(use_case, cfg.raw)               # None (legacy SQL) | [] (toolless) | [tools]
    if cfg.tool_mode in ("agentic", "planned") and loaded_tools is None:
        raise ValueError(f"tool_mode: {cfg.tool_mode} requires a `tools:` list; omit tool_mode for the SQL path")
    if loaded_tools is None and sql is None:
        sql = get_sql_tool(use_case, cfg.default_sql_tool, semantic_layer or load_semantic_layer(use_case))

    skills = load_skills(use_case, cfg.exclude_shared_skills)
    frame_skills = load_named_skills(use_case, cfg.frame_skills) if cfg.frame_skills else skills
    plan_skills = load_named_skills(use_case, cfg.plan_skills) if cfg.plan_skills else frame_skills
    # BUSINESS GLOSSARY (shared + pack, pack wins): reaches BOTH the query side (framing / planning) and
    # the answer side (generate / refine) by riding on the skills text -- no prompt template changes.
    glossary = _p.render_glossary(load_glossary(use_case))
    if glossary:
        skills, frame_skills, plan_skills = (f"{skills}\n\n{glossary}", f"{frame_skills}\n\n{glossary}",
                                             f"{plan_skills}\n\n{glossary}")
    retriever = _build_retriever(cfg, use_case, llm, sql, loaded_tools, log, st.tool_timeout_s, plan_skills,
                                 planner=planner)

    verify_tool = None
    if cfg.judge_verify_tool:
        verify_tool = next((lt for lt in (loaded_tools or []) if lt.label == cfg.judge_verify_tool), None)
        if verify_tool is None or not verify_tool.tool.spec.read_only:
            raise ValueError(f"judge_verify_tool {cfg.judge_verify_tool!r} must name a declared READ-ONLY tool")

    rt = Runtime(
        use_case=use_case, cfg=cfg, llm=llm, eval_llm=eval_llm, retriever=retriever,
        prompt=lambda name: _prompt(use_case, name),
        skills=skills, frame_skills=frame_skills, exemplars=load_exemplars(use_case),
        tools_desc=describe_tools(loaded_tools or []),
        base_instructions=(instructions if instructions is not None
                           else load_instructions(use_case, cfg.exclude_shared_instructions)),
        verify_tool=verify_tool, tool_timeout_s=st.tool_timeout_s, log=log,
        answer_tokens=cfg.max_output_tokens,
    )

    g = StateGraph(State)
    g.add_node("generate", instrument("generate", partial(generate, rt), use_case))
    g.set_entry_point("generate")
    if not loop:
        # NON-LOOP (default): the worker's answer is final. `generate` still frames, gathers (single SQL,
        # deterministic sweep, agentic, planned), reformulates and ESCALATES a blank -- only the judge and
        # refine are gone, so best_score stays -1 and the surfaces report score=None (unscored).
        g.add_edge("generate", END)
        return g.compile()
    g.add_node("evaluate", instrument("evaluate", partial(evaluate, rt), use_case))
    g.add_node("refine", instrument("refine", partial(refine, rt), use_case))
    g.add_conditional_edges("generate", after_generate, {"evaluate": "evaluate", "escalate": END})
    g.add_conditional_edges("evaluate", partial(keep_going, rt), {"refine": "refine", "stop": END})
    g.add_conditional_edges("refine", after_refine, {"evaluate": "evaluate", "stop": END})
    return g.compile()
