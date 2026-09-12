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

from engine.llm_client import (get_llm_client, get_eval_client, model_summary,
                                LLMClient, EmptyResponseError)
from engine.sql_tool import get_sql_tool, SQLTool
from engine.tracing import instrument       # observability is applied from OUTSIDE the nodes
from engine.tools import load_tools, gather_context, describe_tools, run_agentic   # generic tool layer (SQL = one tool)

REPO_ROOT = Path(__file__).resolve().parents[1]
USECASES = REPO_ROOT / "usecases"
SHARED = REPO_ROOT / "shared"


def load_config(use_case: str) -> dict:
    """Merge inherited bases (e.g. shared) then the pack's own config (pack wins)."""
    pack = yaml.safe_load((USECASES / use_case / "config.yaml").read_text(encoding="utf-8")) or {}
    inherits = pack.get("inherits", [])
    if isinstance(inherits, str):                      # `inherits: shared` -> ["shared"], not chars
        inherits = [inherits]
    merged: dict = {}
    for base in inherits:
        base_cfg = (SHARED if base == "shared" else REPO_ROOT / base) / "config.yaml"
        if base_cfg.exists():
            merged.update(yaml.safe_load(base_cfg.read_text(encoding="utf-8")) or {})
    merged.update(pack)
    merged.pop("inherits", None)
    return merged


def load_semantic_layer(use_case: str) -> dict | None:
    """The pack's NATIVE semantic layer (AI+BI/Analyst-backed packs only), read from
    usecases/<pack>/semantic_layer.yaml. FILE PRESENCE marks the pack as AI+BI-backed:
    absent -> None, so the engine never looks for a semantic layer (guard rail for non-AI+BI
    packs -- they run SQL_TOOL=mock or a generic tools: layer). Pack-ONLY: a pack's binding is
    inherently pack-specific, so it is NOT inherited/merged from shared (unlike config.yaml).
    engine/ reads it from HERE, never from env -- any env-vs-pack override is a TEST concern
    handled in the runner scripts (run_local.py / run_evals.py). A present-but-empty file returns
    {} so the platform-selection guard rail in get_sql_tool reports it clearly."""
    f = USECASES / use_case / "semantic_layer.yaml"
    if not f.exists():
        return None
    return yaml.safe_load(f.read_text(encoding="utf-8")) or {}


def _golden_questions(use_case: str) -> list[str]:
    """The questions from a pack's evals/golden_set.yaml (accepts `question` or `input`). Used as the
    default for sample_questions so a pack's 'try these' tracks what it's actually tested on. Best-
    effort: a missing/unparseable file yields []."""
    f = USECASES / use_case / "evals" / "golden_set.yaml"
    if not f.exists():
        return []
    try:
        golden = yaml.safe_load(f.read_text(encoding="utf-8")) or []
    except Exception:
        return []
    out = []
    for case in golden:
        if isinstance(case, dict):
            q = case.get("question") or case.get("input")
            if q and str(q).strip():
                out.append(str(q).strip())
    return out


def pack_manifest(use_case: str) -> dict:
    """Non-secret, client-facing pack metadata: name, description, and sample questions ('try these').
    `sample_questions` come from config `sample_questions:` when set, else default to the golden-set
    questions (so every pack exposes samples that track what it's tested on). Pure metadata -- no loop,
    no model, no creds -- so a UI can fetch it before the first question. Bounded to 20 samples."""
    cfg = load_config(use_case)
    samples = cfg.get("sample_questions") or _golden_questions(use_case)
    samples = [str(s).strip() for s in (samples or []) if str(s).strip()][:20]
    return {"use_case": use_case,
            "name": cfg.get("name", use_case),
            "description": str(cfg.get("description", "")).strip(),
            "sample_task": str(cfg.get("sample_task", "")).strip(),
            "sample_questions": samples}


def _prompt(use_case: str, name: str) -> str:
    """Pack's prompt if present, else fall back to shared/ (override semantics)."""
    for base in (USECASES / use_case / "prompts", SHARED / "prompts"):
        f = base / name
        if f.exists():
            return f.read_text(encoding="utf-8")     # explicit UTF-8: don't let a non-UTF-8 OS locale (e.g. Windows cp1252) mangle non-ASCII artifact text
    raise FileNotFoundError(f"{name} not found in pack or shared")


def load_skills(use_case: str, exclude_shared: list | None = None) -> str:
    """shared skills + pack skills, concatenated (additive). A pack may drop specific SHARED skills
    (by filename stem) via `exclude_shared_skills:` in its config -- e.g. a non-SQL pack excludes
    `sql_safety` so SELECT-only rules aren't injected into a non-SQL agent's prompt. Pack skills are
    never excluded (they are all in-scope by construction)."""
    drop = {str(s).strip().lower() for s in (exclude_shared or [])}
    shared_dir = SHARED / "skills"
    files = []
    for base in (shared_dir, USECASES / use_case / "skills"):
        if base.exists():
            for f in sorted(base.glob("*.md")):
                if base == shared_dir and f.stem.lower() in drop:
                    continue                           # pack opted out of this shared skill
                files.append(f)
    return "\n\n".join(f.read_text(encoding="utf-8").strip() for f in files) if files else "None."


def load_instructions(use_case: str, exclude: list | None = None) -> str:
    """Operator INSTRUCTIONS -- behavioral/policy directives, composed like skills:
    `shared/instructions/*.md` + `usecases/<pack>/instructions/*.md`, concatenated. Distinct from
    skills (domain how-to) so an operator owns them separately and the rubric can grade adherence.
    A pack may drop specific SHARED instruction files (by stem) via `exclude_shared_instructions:`.
    Returns '' when there are none (existing packs -> no block, no behavior change)."""
    drop = {str(s).strip().lower() for s in (exclude or [])}
    shared_dir = SHARED / "instructions"
    files = []
    for base in (shared_dir, USECASES / use_case / "instructions"):
        if base.exists():
            for f in sorted(base.glob("*.md")):
                if base == shared_dir and f.stem.lower() in drop:
                    continue
                files.append(f)
    return "\n\n".join(f.read_text(encoding="utf-8").strip() for f in files)


def _instr_block(*parts: str) -> str:
    """Compose the OPERATOR INSTRUCTIONS block from one or more sources (pack/shared files, a
    build-time override, a per-request runtime string). Returns '' when everything is empty, so no
    block is injected. The single self-describing header works for BOTH the worker ('I must comply')
    and the judge ('penalize violations')."""
    body = "\n".join(p.strip() for p in parts if p and p.strip())
    if not body:
        return ""
    return ("OPERATOR INSTRUCTIONS (authoritative — the answer MUST comply; "
            "penalize violations):\n" + body)


def _format_exemplar(p: dict) -> str:
    """Render ONE exemplar. An explicit `kind:` selects the format; when it's absent we fall back to
    key-sniffing (presence of `sql`) so existing packs are unchanged (backward compatible).
      kind: text_to_sql   -> Q:/SQL:        (fields: question, sql)
      kind: input_output  -> Input:/Output: (fields: input|question, output|answer)
    Prefer an explicit `kind:` once a pack mixes formats -- it's a real discriminator, not a guess."""
    kind = str(p.get("kind", "")).strip().lower()
    if kind == "text_to_sql" or (not kind and "sql" in p):   # verified Q->SQL
        return f"Q: {p['question']}\nSQL: {p['sql']}"
    prompt = p.get("input", p.get("question", ""))     # domain-neutral input->output
    output = p.get("output", p.get("answer", ""))
    return f"Input: {prompt}\nOutput: {output}"


def load_exemplars(use_case: str) -> str:
    """Pack-specific few-shot exemplars, injected into generate. Supports the legacy verified
    question->SQL format AND a domain-neutral input->output format (see _format_exemplar)."""
    d = USECASES / use_case / "exemplars"
    pairs = []
    if d.exists():
        for f in sorted(d.glob("*.yaml")):
            pairs += yaml.safe_load(f.read_text(encoding="utf-8")) or []
    if not pairs:
        return "None."
    return "\n\n".join(_format_exemplar(p) for p in pairs)


def fill(template: str, **kw) -> str:
    """Single-pass placeholder fill: replace each {key} with its kwarg in ONE pass, so a
    substituted value that itself contains "{other}" is never re-interpreted, and literal
    braces in SQL/JSON examples (or unknown {names}) are left untouched (brace-safe)."""
    return re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}",
                  lambda m: str(kw[m.group(1)]) if m.group(1) in kw else m.group(0),
                  template)


def _fill(template: str, instructions: str = "", **kw) -> str:
    """fill() plus operator-instruction placement: a template that contains `{instructions}` controls
    WHERE the block goes; a template that does NOT (every existing pack) gets the block AUTO-PREPENDED
    only when it is non-empty. So packs with no instructions render byte-for-byte as before (empty
    block, nothing prepended) -- zero behavior change -- while an instruction-bearing pack sees the
    block regardless of whether its author added the placeholder."""
    out = fill(template, instructions=instructions, **kw)
    if instructions and "{instructions}" not in template:
        out = instructions + "\n\n" + out
    return out


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
    """A score must be a finite number; round to nearest int (don't truncate 17.9->17).
    Rejects bools (True would become 1) and catches OverflowError from huge JSON integers."""
    if isinstance(value, bool):
        raise ValueError("score must be a number, not a boolean")
    try:
        f = float(value)
    except (TypeError, ValueError, OverflowError):      # OverflowError: 400-digit JSON int
        raise ValueError(f"score is not a finite number: {value!r}")
    if not math.isfinite(f):
        raise ValueError("score must be finite")
    return round(f)


def _first_json_object(text: str) -> str | None:
    """Return the FIRST balanced {...} object in text (tracking JSON strings + escapes), or None.
    A balanced scan beats a greedy `\\{.*\\}` regex, which spans from the first '{' to the LAST
    '}' across multiple objects and then fails to parse -- which would silently re-enable the
    line-form fallback and let an embedded 'SCORE: 18/18' smuggle a passing score (B1 edge)."""
    start = text.find("{")
    if start == -1:
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:            esc = False
            elif c == "\\":    esc = True
            elif c == '"':     in_str = False
            continue
        if c == '"':           in_str = True
        elif c == "{":         depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None                                        # unbalanced -> no complete object


def parse_verdict(raw: str, max_score: int) -> Verdict:
    """Turn a judge reply into a validated Verdict. Accepts either a JSON object
    {"score": N, "reason": "..."} OR the house line form  SCORE: N/<max> - <reason>.
    Strict: rounds (not truncates) numeric scores, rejects a non-finite score, and rejects a
    mismatched denominator (so "18/100" is NOT read as 18/max). Injection defense lives in the
    rubric prompt (the candidate answer is delimited + marked untrusted) plus JSON-first parsing.
    Raises ValueError / ValidationError on anything unparseable or out of range (caller retries)."""
    text = (raw or "").strip()

    obj = None                                         # 1) first balanced JSON object anywhere
    blob = _first_json_object(text)
    if blob:
        try:
            obj = json.loads(blob)
        except (ValueError, TypeError):
            obj = None

    if isinstance(obj, dict) and "score" in obj:
        # A JSON verdict is AUTHORITATIVE: its score wins and a bad score RAISES here (so the
        # caller retries / falls back). We must NOT fall through to line-form, or an embedded
        # "SCORE: 18/18" inside the reason text could smuggle in a passing score (B1).
        verdict = Verdict(score=_coerce_score(obj["score"]),
                          reason=str(obj.get("reason", "")).strip())
    else:                                              # 2) line form: SCORE: N[/denom] - reason
        m = re.search(r"SCORE:\s*([0-9]+(?:\.[0-9]+)?)\s*(?:/\s*([0-9]+))?\s*[-–—:]*\s*([^\n]*)",
                      text, re.IGNORECASE)
        if not m:
            raise ValueError(f"no score found in judge reply: {text[:120]!r}")
        denom = m.group(2)
        if denom is not None and int(denom) != max_score:
            raise ValueError(f"denominator {denom} != max_score {max_score}")
        verdict = Verdict(score=_coerce_score(m.group(1)), reason=m.group(3).strip() or text)

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
    best_feedback: str        # the critique that produced best_answer (refine builds from BEST)
    stall: int                # consecutive refines that did NOT beat best_score (no-progress stop)
    iterations: int
    instructions: str         # per-request OPERATOR instructions (applied only if the pack allows)
    run_id: str               # per-run id; the join key for observability (see engine/tracing.py)


def build_graph(use_case: str, llm: LLMClient | None = None,
                sql: SQLTool | None = None, eval_llm: LLMClient | None = None,
                semantic_layer: dict | None = None, instructions: str | None = None,
                verbose: bool = True):
    cfg = load_config(use_case)
    # max_score = the rubric denominator / validation cap; pass_score = the stop threshold.
    # `threshold` is kept as the backward-compatible alias for pass_score.
    max_score = cfg.get("max_score", 18)
    pass_score = cfg.get("pass_score", cfg.get("threshold", max_score))
    max_iters = cfg.get("max_iters", 4)
    eval_retries = cfg.get("eval_retries", 1)          # extra judge re-asks on unparseable output
    max_stall = cfg.get("max_stall", 2)                # stop after this many refines that DON'T beat
                                                       # best (0 = off). Guards the deterministic
                                                       # refine-from-best "grind to max_iters" case.
    tool_mode = str(cfg.get("tool_mode", "deterministic")).strip().lower()   # deterministic | agentic
    max_tool_steps = cfg.get("max_tool_steps", 4)      # agentic: max model-chosen tool calls per run
    for _name, _val, _min in (("max_score", max_score, 1), ("pass_score", pass_score, 1),
                              ("max_iters", max_iters, 0), ("eval_retries", eval_retries, 0),
                              ("max_stall", max_stall, 0), ("max_tool_steps", max_tool_steps, 0)):
        if type(_val) is not int or _val < _min:       # `type is not int` also rejects bools
            raise ValueError(f"{_name} must be an integer >= {_min}")   # pass_score>=1: 0 would let a fallback-0 "pass"
    if tool_mode not in ("deterministic", "agentic"):
        raise ValueError("tool_mode must be 'deterministic' or 'agentic'")
    if max_tool_steps > 10:                             # bound cost/latency of the gathering loop
        raise ValueError("max_tool_steps must be <= 10")
    if pass_score > max_score:
        raise ValueError("pass_score must be <= max_score")
    if max_iters > 10:                                 # keep graph steps under LangGraph's default recursion limit (25)
        raise ValueError("max_iters must be <= 10 (raise LangGraph's recursion_limit if you truly need more)")
    retry_nudge = (f"\n\nYour previous reply could not be parsed. Reply with EXACTLY "
                   f"one line:  SCORE: N/{max_score} - <short reason>")

    cfg_models = cfg.get("models")                     # pack-level model profiles (env still wins)
    injected_llm = llm                                 # remember whether a worker was injected
    llm = llm or get_llm_client(use_case, cfg_models)  # worker: generate + refine
    # judge: injected eval wins; else reuse an injected worker (so a real worker isn't paired
    # with a mock judge); else resolve independently from env / pack models.
    eval_llm = eval_llm or injected_llm or get_eval_client(use_case, cfg_models)
    # Generic tool layer. `load_tools` returns None when a pack declares NO `tools:` key -> we take
    # the IDENTICAL single-SQL path (existing packs unchanged). A list (even empty) means the pack
    # declared tools explicitly: `tools: []` is a deliberately TOOLLESS agent (no tools AND no SQL).
    loaded_tools = load_tools(use_case, cfg)
    use_sql = loaded_tools is None
    if use_sql and sql is None:
        # Functional config: the semantic layer comes from the PACK (semantic_layer.yaml), read here
        # -- NOT from env. `semantic_layer=` is a dependency-injection seam (like `sql`/`llm`): the
        # runner scripts pass an env-derived override through it for TEST runs; unset -> pack file.
        semantic = semantic_layer or load_semantic_layer(use_case)
        sql = get_sql_tool(use_case, cfg.get("default_sql_tool", "mock"), semantic)
    tools_desc = describe_tools(loaded_tools or [])    # {tools} block for the persona ("None." if legacy/none)
    if tool_mode == "agentic" and loaded_tools is None:
        raise ValueError("tool_mode: agentic requires a `tools:` list; omit tool_mode for the SQL path")
    # Load the act-selection prompt once, only for an agentic tools pack (else the file need not exist).
    act_template = _prompt(use_case, "act.md") if (tool_mode == "agentic" and loaded_tools) else ""
    skills = load_skills(use_case, cfg.get("exclude_shared_skills"))
    exemplars = load_exemplars(use_case)
    # OPERATOR INSTRUCTIONS: injected `instructions=` (DI/runner override) wins, else the pack+shared
    # instruction files. Per-REQUEST instructions (state) are honored ONLY if the pack opts in.
    base_instructions = (instructions if instructions is not None
                         else load_instructions(use_case, cfg.get("exclude_shared_instructions")))
    allow_runtime = bool(cfg.get("allow_runtime_instructions", False))

    def instr_for(s: State) -> str:
        runtime = s.get("instructions", "") if allow_runtime else ""
        return _instr_block(base_instructions, runtime)

    def log(m):
        if verbose:
            print(m)

    log(f"  [models]   {model_summary(cfg_models)}")

    def generate(s: State) -> State:
        # Context source. Legacy: the single SQL call (unchanged). Deterministic tools: the engine
        # runs the declared READ-ONLY tools (model never selects -> injection-safe). Agentic tools:
        # the model chooses which read-only tools to call (opt-in; weaker injection property), still
        # via dispatch(). Either tools path then feeds observations into this same `generate`.
        if use_sql:
            data = sql.ask(s["task"])
        elif tool_mode == "agentic":
            data = run_agentic(s["task"], loaded_tools, llm, act_template,
                               s.get("run_id", "-"), max_tool_steps)
        else:
            data = gather_context(s["task"], loaded_tools, s.get("run_id", "-"))
        answer = llm.complete(_fill(_prompt(use_case, "generate.md"), instructions=instr_for(s),
                                    task=s["task"], data=data, observations=data,
                                    tools=tools_desc, skills=skills,
                                    exemplars=exemplars, revision=0))
        log(f"  [generate] rev0 -> {answer}")
        return {**s, "data": data, "answer": answer, "iterations": 0}

    def evaluate(s: State) -> State:
        # Ground the judge: give it the DATA the answer must be consistent with, so a
        # fabricated number can't satisfy the rubric (the judge can check against evidence).
        base = _fill(_prompt(use_case, "rubric.md"), instructions=instr_for(s),
                     task=s["task"], answer=s["answer"], data=s.get("data", ""),
                     observations=s.get("data", ""),    # {observations} = domain-neutral alias for {data}
                     max_score=max_score)               # rubric's denominator matches the configured scale
        verdict: Verdict | None = None
        for attempt in range(eval_retries + 1):
            prompt = base if attempt == 0 else base + retry_nudge
            try:
                raw = eval_llm.complete(prompt)
            except EmptyResponseError as e:
                # ONLY a blank/whitespace judge reply is retried then falls back (B2). Any OTHER
                # error from complete() (bad config e.g. LLM_MAX_TOKENS=abc, auth, truncation) is
                # NOT a verdict problem -- let it propagate and fail loud, don't mask it as 0 (NB2).
                log(f"  [evaluate] empty judge reply (attempt {attempt + 1}/{eval_retries + 1}): {e}")
                continue
            try:
                verdict = parse_verdict(raw, max_score)
                break
            except (ValueError, ValidationError) as e:  # malformed verdict -> retry, then fall back
                log(f"  [evaluate] unusable verdict "
                    f"(attempt {attempt + 1}/{eval_retries + 1}): {e}")
        if verdict is None:                            # retries exhausted -> safe worst case
            verdict = Verdict(score=0,
                              reason="judge output unparseable; scored 0 to force a refine")
        score = verdict.score
        feedback = f"SCORE: {score}/{max_score} - {verdict.reason}"
        log(f"  [evaluate] score {score}/{max_score}  ({verdict.reason})")
        if score > s.get("best_score", -1):            # keep the champion AND the critique of it
            best_a, best_s, best_f, stall = s["answer"], score, feedback, 0
        else:                                          # no improvement -> count it toward no-progress
            best_a, best_s, best_f = s["best_answer"], s["best_score"], s.get("best_feedback", "")
            stall = s.get("stall", 0) + 1
        return {**s, "score": score, "feedback": feedback, "best_answer": best_a,
                "best_score": best_s, "best_feedback": best_f, "stall": stall}

    def refine(s: State) -> State:
        nxt = s["iterations"] + 1
        # Refine from the BEST answer so far (with the critique that produced it), NOT the latest
        # revision -- otherwise a degraded revision becomes the base and the loop walks downhill,
        # paying full LLM calls to do it. First refine is unchanged (best == latest == rev0).
        base_answer = s.get("best_answer") or s["answer"]
        base_feedback = s.get("best_feedback") or s["feedback"]
        try:
            answer = llm.complete(_fill(_prompt(use_case, "refine.md"), instructions=instr_for(s),
                                        task=s["task"], answer=base_answer, data=s.get("data", ""),
                                        observations=s.get("data", ""),   # domain-neutral alias
                                        feedback=base_feedback, skills=skills, revision=nxt))
        except (RuntimeError, EmptyResponseError) as e:
            # A failed REFINEMENT is non-fatal: we already have a scored best_answer, so keep it and
            # end the loop instead of throwing away good work with a 500 (Bug 1). (A failure on the
            # FIRST generate has nothing to fall back to, so generate stays unguarded -> fatal.)
            log(f"  [refine]   rev{nxt} worker failed: {e}; keeping best so far")
            return {**s, "iterations": max_iters}       # forces keep_going -> stop; best_* preserved
        log(f"  [refine]   rev{nxt} -> {answer}")
        return {**s, "answer": answer, "iterations": nxt}

    def keep_going(s: State) -> str:
        if s["score"] >= pass_score or s["iterations"] >= max_iters:
            return "stop"
        if max_stall and s.get("stall", 0) >= max_stall:   # refines aren't beating best -> stop (Bug 3)
            return "stop"
        return "refine"

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


def initial_state(task: str, instructions: str = "") -> State:
    return {"task": task, "data": "", "answer": "", "feedback": "",
            "score": -1, "best_answer": "", "best_score": -1, "best_feedback": "",
            "stall": 0, "iterations": 0, "instructions": instructions or "",
            "run_id": uuid.uuid4().hex[:12]}
