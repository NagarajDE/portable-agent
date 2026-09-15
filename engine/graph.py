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
from engine.sql_tool import (get_sql_tool, SQLTool, is_no_data, data_hint, no_data,
                             looks_all_zero)
from engine.tracing import instrument       # observability is applied from OUTSIDE the nodes
from engine.tools import (load_tools, gather_context, describe_tools, run_agentic,   # generic tool layer (SQL = one tool)
                          run_planned, strict_bool)

REPO_ROOT = Path(__file__).resolve().parents[1]
USECASES = REPO_ROOT / "usecases"
SHARED = REPO_ROOT / "shared"


def _merge_models(acc: dict, incoming) -> dict:
    """Deep-merge the nested `models:` block PER ROLE (worker/evaluator), so a pack overriding one
    role keeps the other role (and other fields) inherited from a base -- a shallow dict.update would
    replace the whole `models:` block and silently drop inherited roles/fields (M10)."""
    if not isinstance(incoming, dict):
        return acc
    out = dict(acc)
    for role, prof in incoming.items():
        out[role] = {**out[role], **prof} if isinstance(prof, dict) and isinstance(out.get(role), dict) else prof
    return out


def load_config(use_case: str) -> dict:
    """Merge inherited bases (e.g. shared) then the pack's own config (pack wins). Scalars use a
    shallow last-writer-wins; the nested `models:` block is deep-merged per role (see _merge_models)."""
    pack = yaml.safe_load((USECASES / use_case / "config.yaml").read_text(encoding="utf-8")) or {}
    inherits = pack.get("inherits", [])
    if isinstance(inherits, str):                      # `inherits: shared` -> ["shared"], not chars
        inherits = [inherits]
    merged: dict = {}
    models_acc: dict = {}
    for base in inherits:
        base_cfg = (SHARED if base == "shared" else REPO_ROOT / base) / "config.yaml"
        if base_cfg.exists():
            b = yaml.safe_load(base_cfg.read_text(encoding="utf-8")) or {}
            models_acc = _merge_models(models_acc, b.get("models"))
            merged.update(b)
    models_acc = _merge_models(models_acc, pack.get("models"))
    merged.update(pack)
    if models_acc:                                     # restore the deep-merged block over the shallow update
        merged["models"] = models_acc
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


def load_named_skills(use_case: str, names) -> str:
    """Load ONLY the skill files named in `names` (filename, with or without `.md`), in the order given,
    pack dir winning over shared on a stem clash. Used for the FRAMING subset (`frame_skills:`) so the
    query-formulation step sees a focused set (the value->column maps) even when the pack ships many
    answer-side skills. Unknown names are skipped; an empty/all-unknown list yields 'None.' (framing then
    has nothing to bind and falls back to the raw question -- safe)."""
    if isinstance(names, str):
        names = [names]
    wanted = []
    for n in (names or []):
        s = str(n).strip()
        if s:
            wanted.append((s[:-3] if s.lower().endswith(".md") else s).lower())
    if not wanted:
        return "None."
    found = {}
    for base in (USECASES / use_case / "skills", SHARED / "skills"):   # pack first -> pack wins
        if base.exists():
            for f in base.glob("*.md"):
                found.setdefault(f.stem.lower(), f)
    files = [found[w] for w in wanted if w in found]
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
        # Parsed in TWO steps so a malformed number can't be silently skipped (M12). The `(?![\w.])`
        # boundary rejects a garbled score token (18e3, 18abc). CRUCIALLY, a denominator is not just
        # "optional": if a '/' follows the score, a VALID clean denominator is REQUIRED -- otherwise a
        # single optional group would BACKTRACK past a bad denominator ('18/18e3') and let '/18e3'
        # become reason text with the check skipped, smuggling a passing 18.
        m = re.search(r"SCORE:\s*([0-9]+(?:\.[0-9]+)?)(?![\w.])", text, re.IGNORECASE)
        if not m:
            raise ValueError(f"no score found in judge reply: {text[:120]!r}")
        rest = text[m.end():]
        dm = re.match(r"\s*/\s*([0-9]+(?:\.[0-9]+)?)(?![\w.])", rest)
        if re.match(r"\s*/", rest) and dm is None:     # a slash with NO valid denominator -> reject
            raise ValueError(f"malformed denominator after score in: {text[:120]!r}")
        if dm is not None:
            if float(dm.group(1)) != max_score:        # 18.5 / 100 / ... rejected; 18 or 18.0 pass
                raise ValueError(f"denominator {dm.group(1)} != max_score {max_score}")
            rest = rest[dm.end():]
        reason_m = re.match(r"\s*[-–—:]*\s*([^\n]*)", rest)
        reason = (reason_m.group(1).strip() if reason_m else "") or text
        verdict = Verdict(score=_coerce_score(m.group(1)), reason=reason)

    if verdict.score > max_score:
        raise ValueError(f"score {verdict.score} exceeds max {max_score}")
    return verdict


# A reformulation must NARROW a question, never SUBSTITUTE it. Two independent guards, because the
# model that produced the blank cannot be trusted to police its own scope: (1) it may REFUSE with this
# token when the subject simply isn't in the data (shared/prompts/reformulate.md), and (2) the engine
# checks DETERMINISTICALLY that a distinctive word of the original survived. The failure this prevents
# was observed live: "average employee salary by department" came back as "average inventory value per
# plant", which RETURNS ROWS -> looks grounded -> the judge scored the honest "not in this data" answer
# 16/18. Answering a question nobody asked, from real rows, is worse than reporting the gap.
_REFUSE = "NO_REFORMULATION"
# Words too generic to prove the subject survived. Kept deliberately small; every word added makes the
# guard STRICTER, and a false "drifted" verdict is SAFE (the run escalates to a human) while a false
# "kept it" is the bug itself. The >=3-letter tier is here BECAUSE the content-word floor is 3 (so a
# short subject like 'tax'/'fee' is visible): without it, pure function words ('the', 'per', 'top')
# would look distinctive. Only ever add true function/rank words here -- never a domain noun.
_GENERIC = {
    "what", "which", "when", "where", "many", "much", "total", "average", "list", "show", "give",
    "count", "value", "values", "amount", "amounts", "number", "numbers", "percent", "percentage",
    "share", "data", "field", "fields", "record", "records", "rows", "report", "across", "each",
    "have", "does", "were", "been", "with", "from", "that", "this", "there", "their", "your",
    "last", "over", "into", "most", "than", "then", "also", "only", "some", "them", "they", "will",
    # >=3-letter function / rank words (floor is 3): dropping any of these is never a subject change.
    "the", "and", "for", "are", "was", "has", "had", "our", "out", "per", "who", "you", "its",
    "all", "any", "one", "two", "top", "how", "why", "did", "not", "but", "may", "can", "via",
}


def _stem(word: str) -> str:
    """Fold a trailing plural to a common stem so a plural<->singular rewrite counts as the SAME subject
    ('vendors'->'vendor', 'salaries'->'salary', 'categories'->'category'). Linguistically rough on
    purpose: the original and the rewrite are stemmed IDENTICALLY, so the comparison stays consistent
    even when the stem isn't a real word ('analysis'->'analysi' on both sides still matches itself)."""
    if len(word) >= 6 and word.endswith("ies"):
        return word[:-3] + "y"                        # salaries -> salary, categories -> category
    if len(word) >= 4 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]                             # vendors -> vendor, fees -> fee (address stays)
    return word


# The generic set STEMMED, because _content_words stems every word BEFORE the membership test -- an
# unstemmed set would let a stemmed plural leak through as a fake 'subject' (F4: 'rows'->'row',
# 'does'->'doe', 'across'->'acros' are not literally in _GENERIC). Stemming the set closes that.
_GENERIC_STEMS = {_stem(w) for w in _GENERIC}

# Split a question into its MEASURE (what is being counted/summed) and its DIMENSION (what it's grouped
# by). Only the FIRST marker matters: everything before it is the measure. Used so a rewrite that keeps
# only the dimension ('salary by dept' -> 'inventory value by dept') is caught as drift (F3) -- 'dept'
# surviving is not enough; the MEASURE must survive.
_DIM = re.compile(r"\b(?:broken down by|grouped by|group by|for each|by|per|across)\b", re.IGNORECASE)


def _content_words(text: str) -> set[str]:
    """Distinctive (>=3-letter, non-generic, plural-folded) words -- the crude 'subject' of a question.
    Floor is 3 so short nouns ('tax'/'fee'/'pay') are visible; SHORT ALL-CAPS acronyms (HR, IT, EMEA)
    are pulled from the ORIGINAL casing too, so a subject made only of an acronym isn't invisible (F5)."""
    t = text or ""
    words = {_stem(w) for w in re.findall(r"[a-z]{3,}", t.lower())}
    words |= {a.lower() for a in re.findall(r"\b[A-Z]{2,5}\b", t)}   # HR / IT / QA / EMEA (case-marked)
    return {w for w in words if w not in _GENERIC_STEMS}


def _measure_words(text: str) -> set[str]:
    """The content words of the MEASURE -- the part before the first 'by/per/grouped by/...' marker
    (the whole question when there is no marker)."""
    return _content_words(_DIM.split(text or "", maxsplit=1)[0])


def _pinned_codes(text: str) -> set[str]:
    """Literal identifiers the user PINNED: tokens mixing letters and digits (ZZ999, PO12345, QQQ404ZZ).
    Silently dropping one WIDENS the population the answer is about, so a rewrite that loses every
    pinned code is answering about a different set of rows.

    Known limitations, ALL in the SAFE direction (they escalate as no_data, never false-pass):
      * a bare-number filter (a year like 2026) is NOT pinned -- 'top 10' is a rank, not a filter, and
        there is no way to tell the two apart. The subject check is the only guard there.
      * a period code (FY26, Q1FY26, 1H26) IS pinned, so if the rewrite RE-EXPRESSES it in words
        ('FY26' -> 'fiscal 2026') the match fails and the run escalates with an honest 'no rows for
        that period' message instead of recovering. Accepted on purpose: exempting period codes would
        let a rewrite DROP the period entirely ('FY26 spend' -> 'spend', all years) and pass -- the
        exact widening false-pass this guard exists to stop."""
    pat = r"\b(?=[A-Za-z0-9]*[A-Za-z])(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{3,}\b"
    return {w.upper() for w in re.findall(pat, text or "")}


def _keeps_subject(original: str, rewritten: str) -> bool:
    """True when the MEASURE of the original survives the rewrite -- the subject was NARROWED, not
    SUBSTITUTED. Checks the MEASURE (the part before 'by/per/...'), not the whole question, so a rewrite
    that keeps only the DIMENSION is caught: 'employee salary by department' -> 'inventory value by
    department' is drift even though 'department' survives (F3). Uses ANY-overlap on the measure, not a
    subset, so a legitimate narrowing that drops an adjective/synonym isn't over-escalated; a DROPPED
    FILTER word inside a preserved measure ('active contract value' -> 'contract value') is therefore
    NOT caught here -- that residual is left to the prompt-level NO_REFORMULATION refusal.
    Fail-open when the original has no measure word at all (a genuinely subjectless 'what is the total?')."""
    measure = _measure_words(original)
    if measure:
        return bool(measure & _content_words(rewritten))
    whole = _content_words(original)                 # no distinct measure -> fall back to any-overlap
    return not whole or bool(whole & _content_words(rewritten))


def _keeps_pinned(original: str, rewritten: str) -> bool:
    """True when EVERY pinned identifier survives the rewrite (subset, not intersection). Dropping even
    one widens the population: 'compare PO123 and PO456' -> 'show PO123' is drift, not a narrowing (F1).
    Kept SEPARATE from the subject check because the two failures mean different things to a human: a
    substituted subject means this data cannot answer the question at all, while a dropped identifier
    means the subject is fine and only that entity has no rows."""
    pinned = _pinned_codes(original)
    return pinned <= _pinned_codes(rewritten)        # empty set is a subset of anything -> fail-open


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
    refine_failed: bool       # a refine worker call raised -> route straight to END (skip re-evaluate)
    grounded: bool            # did retrieval yield usable data? False -> escalate (skip the judge)
    status: str               # ""=normal; "no_data"=retrieval blank after retries; "out_of_scope"=the
                              #   question isn't answerable from this data at all. Both -> a human.
    data_retries: int         # reformulate-and-retry attempts spent before giving up (observability)
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
    tool_mode = str(cfg.get("tool_mode", "deterministic")).strip().lower()   # deterministic|agentic|planned
    max_tool_steps = cfg.get("max_tool_steps", 4)      # agentic: max model-chosen tool calls per run
    max_plan_steps = cfg.get("max_plan_steps", 8)      # planned: max steps in the validated DAG (dispatches)
    max_replans = cfg.get("max_replans", 2)            # planned: max plan revisions on a step failure
    max_ref_chars = cfg.get("max_ref_chars", 2000)     # planned: cap on a {{sN}} step-to-step handoff summary
    max_data_retries = cfg.get("max_data_retries", 1)  # blank retrieval -> reformulate+re-query this many
                                                       # times before escalating as no_data (0 = at once)
    # OPT-IN: treat a single all-zero/NULL row (COUNT(*)=0, SUM(...)=NULL) as a blank retrieval. Default
    # OFF because it is indistinguishable from a TRUE zero -- escalating would be WRONG for a pack where
    # "0" is a real answer. Turn it on for packs where 0 almost always means a filter or status label
    # matched nothing (see engine/sql_tool.py: looks_all_zero).
    zero_is_no_data = strict_bool(cfg.get("zero_is_no_data"), False)
    # OPT-IN: use the pack SKILLS to make the question PRECISE before the first retrieval (skill-informed
    # query formulation). Default OFF, so packs that don't enable it are byte-for-byte unchanged. See
    # _frame_question -- it is fail-safe (any drift/error falls back to the raw question).
    frame_query = strict_bool(cfg.get("frame_query"), False)
    # OPT-IN safety net (reactive value recovery): the OPTIONAL note/skills make the FIRST query precise;
    # if a query still comes back EMPTY, look up the real distinct values of these declared columns from the
    # live view and re-ask with the exact stored value (see the reformulate step). Fires ONLY on a blank
    # retrieval, once per session (cached), and is a NO-OP unless the SQL tool exposes distinct_values()
    # (Cortex only -> MOCK stays deterministic). Presence of a non-empty list = enabled; unset = unchanged.
    recover_value_dims = cfg.get("recover_value_dims") or []          # pack-authored 'TABLE.DIM' paths
    for _name, _val, _min in (("max_score", max_score, 1), ("pass_score", pass_score, 1),
                              ("max_iters", max_iters, 0), ("eval_retries", eval_retries, 0),
                              ("max_stall", max_stall, 0), ("max_tool_steps", max_tool_steps, 0),
                              ("max_data_retries", max_data_retries, 0),
                              ("max_plan_steps", max_plan_steps, 1), ("max_replans", max_replans, 0),
                              ("max_ref_chars", max_ref_chars, 200)):
        if type(_val) is not int or _val < _min:       # `type is not int` also rejects bools
            raise ValueError(f"{_name} must be an integer >= {_min}")   # pass_score>=1: 0 would let a fallback-0 "pass"
    if tool_mode not in ("deterministic", "agentic", "planned"):
        raise ValueError("tool_mode must be 'deterministic', 'agentic', or 'planned'")
    if max_tool_steps > 10:                             # bound cost/latency of the gathering loop
        raise ValueError("max_tool_steps must be <= 10")
    if max_data_retries > 5:                            # bound cost/latency of the reformulate-retry loop
        raise ValueError("max_data_retries must be <= 5")
    if max_plan_steps > 20:                             # bound cost/latency (DoS) of a multi-step plan
        raise ValueError("max_plan_steps must be <= 20")
    if max_replans > 5:                                 # bound plan-revision churn
        raise ValueError("max_replans must be <= 5")
    if max_ref_chars > 8000:                            # keep an untrusted handoff summary bounded
        raise ValueError("max_ref_chars must be <= 8000")
    if pass_score > max_score:
        raise ValueError("pass_score must be <= max_score")
    if max_iters > 10:                                 # keep graph steps under LangGraph's default recursion limit (25)
        raise ValueError("max_iters must be <= 10 (raise LangGraph's recursion_limit if you truly need more)")
    if recover_value_dims and (not isinstance(recover_value_dims, list)
            or not all(isinstance(d, str) and d.strip() for d in recover_value_dims)):
        raise ValueError("recover_value_dims must be a list of 'TABLE.DIM' dimension paths")
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
    if tool_mode in ("agentic", "planned") and loaded_tools is None:
        raise ValueError(f"tool_mode: {tool_mode} requires a `tools:` list; omit tool_mode for the SQL path")
    # Load the act-selection prompt once, only for an agentic tools pack (else the file need not exist).
    act_template = _prompt(use_case, "act.md") if (tool_mode == "agentic" and loaded_tools) else ""
    # Load the plan/replan prompts once, only for a planned tools pack (else the files need not exist).
    plan_template = _prompt(use_case, "plan.md") if (tool_mode == "planned" and loaded_tools) else ""
    replan_template = _prompt(use_case, "replan.md") if (tool_mode == "planned" and loaded_tools) else ""
    skills = load_skills(use_case, cfg.get("exclude_shared_skills"))
    # FRAMING subset: the query-formulation step sees only the skills a pack lists in `frame_skills:`
    # (the value->column maps), so porting many answer-side skills doesn't bloat the framing prompt or
    # tempt it to over-reach. Unset -> the FULL skill set (backward compatible: goa_spend unchanged).
    frame_skills_text = (load_named_skills(use_case, cfg.get("frame_skills"))
                         if cfg.get("frame_skills") else skills)
    # PLANNING subset (planned mode): the planner sees only `plan_skills:` (the decomposition guidance),
    # keeping the plan prompt lean; unset -> `frame_skills` if set, else the FULL skill set.
    plan_skills_text = (load_named_skills(use_case, cfg.get("plan_skills"))
                        if cfg.get("plan_skills") else frame_skills_text)
    exemplars = load_exemplars(use_case)
    # OPERATOR INSTRUCTIONS: injected `instructions=` (DI/runner override) wins, else the pack+shared
    # instruction files. Per-REQUEST instructions (state) are honored ONLY if the pack opts in.
    base_instructions = (instructions if instructions is not None
                         else load_instructions(use_case, cfg.get("exclude_shared_instructions")))
    allow_runtime = strict_bool(cfg.get("allow_runtime_instructions"), False)   # strict: no fail-open (H4)

    def instr_for(s: State) -> str:
        runtime = s.get("instructions", "") if allow_runtime else ""
        return _instr_block(base_instructions, runtime)

    def log(m):
        if verbose:
            print(m)

    log(f"  [models]   {model_summary(cfg_models)}")

    def _retrieve(question: str, run_id: str) -> str:
        # Context source. Legacy: the single SQL call (unchanged). Deterministic tools: the engine
        # runs the declared READ-ONLY tools (model never selects -> injection-safe). Agentic tools:
        # the model chooses which read-only tools to call (opt-in; weaker injection property), still
        # via dispatch(). Each path may return the NO_DATA sentinel to mark a blank retrieval.
        if use_sql:
            out = sql.ask(question)
        elif tool_mode == "agentic":
            out = run_agentic(question, loaded_tools, llm, act_template, run_id, max_tool_steps)
        elif tool_mode == "planned":
            # Multi-step: the model commits a VALIDATED DAG; the engine executes it (chaining {{sN}}
            # results), replanning on failure. Self-corrects via replan -> excluded from the outer
            # reformulate loop and from _frame_question (the planner owns query formulation).
            out = run_planned(question, loaded_tools, llm, plan_template, replan_template,
                              plan_skills_text, run_id, use_case, max_plan_steps, max_replans,
                              max_ref_chars)
        elif not loaded_tools:
            # `tools: []` is a DELIBERATELY toolless agent (answers from skills alone). It has no
            # retrieval, so there is no such thing as a blank one -- never escalate it. Distinct wording
            # from gather_context's "No tool observations." hint so the two paths can't be confused.
            out = "No tools configured; answered from skills alone."
        else:
            out = gather_context(question, loaded_tools, run_id)
        if zero_is_no_data and looks_all_zero(out):    # opt-in: a lone row of zeros is not evidence
            return no_data("The query returned a single all-zero/NULL row -- a filter or a status "
                           "label probably matched nothing. Check which values are actually present.")
        return out

    _kv_cache = {}                                        # lazy, once-per-graph: {"block": <str>}

    def _recover_values_block() -> str:
        # REACTIVE value recovery (used only after a BLANK retrieval): fetch the real distinct values of the
        # pack's declared columns ONCE and format them for the reformulate step, so the reword can bind the
        # user's word to the EXACT stored value (e.g. "active" -> Executed/Approved, "instruments" ->
        # Instrument). NO-OP unless recover_value_dims is set AND the tool exposes distinct_values() (Cortex
        # only -> mock/agentic packs get ""). FAIL-SAFE: any error caches "" so the retry just rewords as
        # before. Cached so it costs one small query per column per session, and ONLY when an empty result
        # actually triggers it -- questions that return rows on the first try pay nothing.
        if "block" in _kv_cache:
            return _kv_cache["block"]
        block = ""
        lookup = getattr(sql, "distinct_values", None) if recover_value_dims else None
        if lookup:
            try:
                found = lookup(recover_value_dims) or {}
            except Exception as e:                        # best-effort: recovery must never break a run
                log(f"  [recover] value lookup failed: {e}; rewording without live values")
                found = {}
            if found:
                lines = [f"- {path.split('.')[-1]}: " + ", ".join(vals) for path, vals in found.items()]
                block = ("STORED VALUES (the real values currently in these columns; the user's word may "
                         "differ -- plural/singular, code vs label -- so map it to the EXACT value shown):\n"
                         + "\n".join(lines))
                log(f"  [recover] looked up values for {len(found)} column(s)")
        _kv_cache["block"] = block
        return block

    def _frame_question(task: str) -> str:
        # Skill-informed QUERY FORMULATION (runs BEFORE retrieval): rewrite the user's question into a
        # precise text-to-SQL request using the pack SKILLS -- e.g. bind "the IT Software category" to the
        # dimension that actually holds it -- so the tool does not guess the wrong column. This is the ONE
        # place skills reach the QUERY; generate.md/refine.md see them only when WRITING the answer, AFTER
        # retrieval (too late to change what rows come back). FAIL-SAFE: any error, an empty reply, or a
        # rewrite that DRIFTS (subject substituted or a pinned id dropped -- the same guards the
        # reformulate-retry uses) falls back to the RAW task, so framing can only ADD precision, never
        # CREATE an escalation (worst case == today's behavior).
        try:
            framed = llm.complete(_fill(_prompt(use_case, "frame.md"),
                                        task=task, skills=frame_skills_text)).strip()
        except (RuntimeError, EmptyResponseError, ValueError, KeyError) as e:
            log(f"  [frame] failed: {e}; using original question")
            return task
        if not framed or not (_keeps_subject(task, framed) and _keeps_pinned(task, framed)):
            log(f"  [frame] discarded (empty or drifted); using original question")
            return task
        if framed != task:
            log(f"  [frame] {task}  ->  {framed}")
        return framed

    def generate(s: State) -> State:
        run_id = s.get("run_id", "-")
        # Skill-informed query formulation (opt-in, fail-safe): use the pack SKILLS to make the question
        # precise BEFORE retrieval, so the query/tool step doesn't have to guess a dimension. Applies to
        # any path the QUESTION drives -- the SQL call, the model-driven AGENTIC loop, or deterministic
        # tools that take the question as input -- but NOT a toolless pack (nothing to retrieve). Falls
        # back to the raw task on drift/error.
        analyst_q = s["task"]
        frames_retrieval = (use_sql or tool_mode == "agentic"
                            or (tool_mode == "deterministic" and bool(loaded_tools)))
        if frame_query and frames_retrieval:
            analyst_q = _frame_question(s["task"])
        # First retrieval is UNGUARDED: a hard error (bad SQL, network) is fatal, matching the
        # "a failed first generate is fatal" rule -- there is nothing to fall back to.
        data = _retrieve(analyst_q, run_id)
        retries = 0
        # A BLANK retrieval does NOT prove the data is missing -- it may be a misread question or a
        # wrong/empty query. On the paths that fire EXACTLY ONCE (the single SQL call, the deterministic
        # tool sweep) give it a bounded chance to self-correct: reformulate the question from the tool's
        # hint and re-retrieve, BEFORE concluding a genuine gap. If that recovers data it was a misread;
        # if it stays blank it is a real gap and the run escalates. Agentic is excluded -- it already had
        # `max_tool_steps` chances to self-correct.
        # The rewrite is NOT trusted blindly: a REFUSAL or an off-subject DRIFT is discarded (see
        # _keeps_subject) so the run escalates instead of answering a different, answerable question.
        can_retry = use_sql or tool_mode == "deterministic"
        reason = "no_data"                                # why we'd escalate: blank vs. wrong question
        while can_retry and is_no_data(data) and retries < max_data_retries:
            retries += 1
            hint = data_hint(data)
            off_scope = hint or "The question asks about a subject this data does not cover."
            try:
                # F7: the hint is UNTRUSTED tool output -- bound its length so a hostile/runaway tool
                # clarification can't dominate the reformulation prompt; the prompt itself frames it as
                # data, not instructions. The drift guards (_keeps_subject/_keeps_pinned) are the
                # backstop, and the reformulation's OUTPUT is still a read-only text-to-SQL request.
                new_q = llm.complete(_fill(_prompt(use_case, "reformulate.md"),
                                           task=s["task"], feedback=hint[:600],
                                           known_values=_recover_values_block())).strip()
            except (RuntimeError, EmptyResponseError, ValueError, KeyError) as e:
                # a retry that ERRORS is not fatal (unlike the first retrieval): keep the blank marker
                # and let the loop exhaust its budget, then escalate rather than 500.
                log(f"  [retry {retries}] failed: {e}; keeping no-data")
                data = no_data(hint)
                continue
            if not new_q or new_q.upper().lstrip("*_# ").startswith(_REFUSE):
                log(f"  [retry {retries}] reformulation REFUSED: subject not in this data")
                data, reason = no_data(off_scope), "out_of_scope"
                break                                  # a refusal is final -- don't burn more retries
            kept_subject = _keeps_subject(s["task"], new_q)   # evaluate each guard ONCE
            kept_pinned = _keeps_pinned(s["task"], new_q)
            if not kept_subject or not kept_pinned:
                # WHY it drifted decides what the human is told. Subject SUBSTITUTED -> this data can't
                # answer the question at all. Identifier DROPPED -> the subject is fine, that entity
                # just has no rows; widening it would answer about a different population.
                reason = "out_of_scope" if not kept_subject else "no_data"
                log(f"  [retry {retries}] reformulation DRIFTED ({reason}) -> {new_q}")
                data = no_data(off_scope if reason == "out_of_scope" else
                               (hint or "No rows matched the identifier in the question."))
                break                                  # would answer a DIFFERENT question -> escalate
            log(f"  [retry {retries}] reformulated -> {new_q}")
            try:
                data = _retrieve(new_q, run_id)
            except (RuntimeError, ValueError, KeyError) as e:
                log(f"  [retry {retries}] re-retrieval failed: {e}; keeping no-data")
                data = no_data(hint)
        grounded = not is_no_data(data)
        obs = data if grounded else data_hint(data)    # show the worker the hint, never the raw sentinel
        answer = llm.complete(_fill(_prompt(use_case, "generate.md"), instructions=instr_for(s),
                                    task=s["task"], data=obs, observations=obs,
                                    tools=tools_desc, skills=skills,
                                    exemplars=exemplars, revision=0))
        log(f"  [generate] rev0 (grounded={grounded}, retries={retries}) -> {answer}")
        if not grounded:
            # ESCALATE: keep the honest answer for the human, but it is NOT a scored/passing answer.
            # best_score stays -1 (the judge never runs), and the surfaces report the REASON so a human
            # can act: "no_data" (the query ran, nothing there -- check filters/freshness) vs.
            # "out_of_scope" (this data can't answer that question -- ask a different agent). Neither
            # is ever a score.
            return {**s, "data": obs, "answer": answer, "best_answer": answer, "iterations": 0,
                    "grounded": False, "status": reason, "data_retries": retries}
        return {**s, "data": data, "answer": answer, "iterations": 0,
                "grounded": True, "status": "", "data_retries": retries}

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
            # end the loop instead of throwing away good work with a 500 (Bug 1). Route STRAIGHT to
            # END via `refine_failed` -- NOT back through evaluate -- so we don't spend (and risk) an
            # extra judge call on the unchanged answer (H6). (A failure on the FIRST generate has
            # nothing to fall back to, so generate stays unguarded -> fatal.)
            log(f"  [refine]   rev{nxt} worker failed: {e}; keeping best so far")
            return {**s, "refine_failed": True}
        log(f"  [refine]   rev{nxt} -> {answer}")
        return {**s, "answer": answer, "iterations": nxt, "refine_failed": False}

    def keep_going(s: State) -> str:
        if s["score"] >= pass_score or s["iterations"] >= max_iters:
            return "stop"
        if max_stall and s.get("stall", 0) >= max_stall:   # refines aren't beating best -> stop (Bug 3)
            return "stop"
        return "refine"

    def after_generate(s: State) -> str:
        # No usable data after reformulate-and-retry -> hand the honest answer to a human as a distinct
        # outcome; do NOT run the judge (it can't create data and must never award a pass on no data).
        return "escalate" if not s.get("grounded", True) else "evaluate"

    def after_refine(s: State) -> str:
        # A failed refine ends the run immediately (best_* already preserved); a successful one goes
        # to evaluate to score the new revision. This makes the failed-refine path skip evaluate (H6).
        return "stop" if s.get("refine_failed") else "evaluate"

    # Observability is applied here, from OUTSIDE the nodes: instrument() wraps each node
    # to emit a timed event (or returns it unchanged when TRACER=none -> zero overhead).
    g = StateGraph(State)
    g.add_node("generate", instrument("generate", generate, use_case))
    g.add_node("evaluate", instrument("evaluate", evaluate, use_case))
    g.add_node("refine", instrument("refine", refine, use_case))
    g.set_entry_point("generate")
    g.add_conditional_edges("generate", after_generate, {"evaluate": "evaluate", "escalate": END})
    g.add_conditional_edges("evaluate", keep_going, {"refine": "refine", "stop": END})
    g.add_conditional_edges("refine", after_refine, {"evaluate": "evaluate", "stop": END})
    return g.compile()


def initial_state(task: str, instructions: str = "") -> State:
    return {"task": task, "data": "", "answer": "", "feedback": "",
            "score": -1, "best_answer": "", "best_score": -1, "best_feedback": "",
            "stall": 0, "iterations": 0, "refine_failed": False,
            "grounded": True, "status": "", "data_retries": 0,
            "instructions": instructions or "", "run_id": uuid.uuid4().hex[:12]}
