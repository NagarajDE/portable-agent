"""
ENGINE / retrieval -- ONE abstraction for "get the evidence": a typed `Retrieval` result and a family
of `Retriever` STRATEGIES (single-SQL · deterministic tool sweep · agentic ReAct · planned DAG ·
toolless). The loop never branches on a mode again; it asks the strategy two questions --
`frames_question` (run skill-informed framing first?) and `reformulates` (can a blank be recovered by
rephrasing and re-retrieving?) -- and consumes one `Retrieval`.

GROUNDEDNESS IS TYPED HERE, not encoded as a magic string. Adapters that return plain text (the
SQLTool protocol, tool outputs) may carry the NO_DATA sentinel as their wire format; `Retrieval.from_text`
is the ONLY place that sentinel is interpreted, so no other module sniffs strings.

EVERY strategy that touches an external system goes through `dispatch()` -- including the single-SQL
path, which previously bypassed it (no timeout/redaction/bounds). One trust boundary, one path.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable

from pydantic import BaseModel, ConfigDict

from engine.binding import (filter_columns, resolve_dims, render_values_block,
                            RECOVERY_HEADER, CATALOG_HEADER)
from engine.sql_tool import SQLTool, is_no_data, data_hint, looks_all_zero
from engine.tools.base import ToolContext, ToolResult, ToolSpec
from engine.tools.dispatch import dispatch
from engine.tools import gather_context, run_agentic, run_planned


# --- the typed result ----------------------------------------------------------------------------
@dataclass(frozen=True)
class Retrieval:
    text: str            # evidence shown to worker + judge (rows; or the hint when empty)
    empty: bool          # no usable evidence -> the run must not be scored as grounded
    hint: str = ""       # why nothing came back (tool clarification), feeds reformulation

    @classmethod
    def from_text(cls, s) -> "Retrieval":
        """Convert a text-protocol result. The NO_DATA sentinel is interpreted HERE and nowhere else."""
        s = "" if s is None else str(s)
        if is_no_data(s):
            h = data_hint(s)
            return cls(text=h, empty=True, hint=h)
        return cls(text=s, empty=not s.strip(), hint="")

    @classmethod
    def blank(cls, hint: str = "") -> "Retrieval":
        return cls(text=hint, empty=True, hint=hint)


# --- strategies ----------------------------------------------------------------------------------
class Retriever:
    """Base strategy. Subclasses implement `_retrieve`; the base applies the pack's zero-row rule."""
    frames_question = False       # run skill-informed framing on the question first?
    reformulates = False          # may a blank be recovered by rephrasing + re-retrieving?

    def __init__(self, zero_is_no_data: bool = False):
        self._zero = zero_is_no_data

    def retrieve(self, question: str, run_id: str = "-") -> Retrieval:
        r = self._retrieve(question, run_id)
        if self._zero and not r.empty and looks_all_zero(r.text):
            return Retrieval.blank("The query returned a single all-zero/NULL row -- a filter or a "
                                   "status label probably matched nothing. Check which values are "
                                   "actually present.")
        return r

    def _retrieve(self, question: str, run_id: str) -> Retrieval:
        raise NotImplementedError

    def recovery_block(self) -> str:
        """Extra context for the reformulate step (e.g. real stored values). '' by default."""
        return ""

    def catalog_block(self) -> str:
        """Extra context for the FRAMING step (a low-cardinality value catalog). '' by default."""
        return ""


class _SqlInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str


class _SqlAsTool:
    """Wrap any SQLTool (mock fixture, Cortex Analyst, an injected test double) as a Tool so the single-
    SQL path runs through dispatch() like every other tool: validated, timed, redacted, bounded."""
    def __init__(self, sql: SQLTool):
        self.spec = ToolSpec(name="sql", description="Text-to-SQL retrieval over the pack's semantic layer.",
                             read_only=True, input_model=_SqlInput)
        self._sql = sql

    def run(self, input: dict, ctx: ToolContext) -> ToolResult:
        text = self._sql.ask(input["question"])
        return ToolResult(ok=True, output="" if text is None else str(text))


class SqlRetriever(Retriever):
    """The single text-to-SQL call (legacy packs with no `tools:`). Frames and reformulates.

    Term->value binding (see engine/binding.py) is HYBRID: hand-declared `recover_dims` are looked up
    first (a pack's deliberate few), then dims are AUTO-derived from the predicates of the SQL the tool
    actually ran, so a wrong literal on ANY column can be corrected without pre-enumerating it. An
    optional low-cardinality `value_catalog` is profiled once for the framing step."""
    frames_question = True
    reformulates = True

    def __init__(self, sql: SQLTool, *, zero_is_no_data=False, recover_dims=None, value_catalog=None,
                 timeout_s: float = 30.0, log: Callable[[str], None] = lambda m: None):
        super().__init__(zero_is_no_data)
        self.sql = sql
        self._tool = _SqlAsTool(sql)
        self._dims = list(recover_dims or [])
        self._catalog_cfg = dict(value_catalog or {})
        self._timeout = timeout_s
        self._log = log
        self._vals: dict[str, list[str]] = {}          # per-dimension distinct-value cache
        self._catalog: str | None = None

    def _retrieve(self, question: str, run_id: str) -> Retrieval:
        res = dispatch(self._tool, {"question": question}, ToolContext(run_id=run_id, timeout_s=self._timeout))
        if not res.ok:
            # A hard retrieval failure is a real error (bad SQL, auth, timeout) -- surface it. The FIRST
            # retrieval is fatal by design (nothing to fall back to); a retry's failure is caught by the
            # caller and treated as still-blank.
            kind = res.error.kind if res.error else "error"
            raise RuntimeError(f"sql retrieval failed ({kind}): {res.error.message if res.error else ''}")
        return Retrieval.from_text(res.output)

    # --- term -> stored-value binding ----------------------------------------------------------------
    def _lookup(self, paths: list[str], limit: int = 50) -> dict[str, list[str]]:
        """Distinct values for `paths`, cached per path. Best-effort; a tool without distinct_values()
        (mock) yields {} so binding degrades to skills-only."""
        lookup = getattr(self.sql, "distinct_values", None)
        want = [p for p in paths if p not in self._vals]
        if lookup and want:
            try:
                for p, vals in (lookup(want, limit=limit) or {}).items():
                    self._vals[p] = list(vals)
            except Exception as e:                      # never break a run over a lookup
                self._log(f"  [recover] value lookup failed: {e}; continuing without live values")
        return {p: self._vals[p] for p in paths if self._vals.get(p)}

    def _auto_dims(self) -> list[str]:
        """Dims implicated by the LAST query's string predicates, resolved against the view's dimension
        catalog -- the general, no-hand-authoring path. [] when the tool exposes neither."""
        sql = getattr(self.sql, "last_sql", "") or ""
        catalog = getattr(self.sql, "dimension_paths", None)
        if not sql or not catalog:
            return []
        try:
            return resolve_dims(filter_columns(sql), catalog() or [])
        except Exception:
            return []

    def recovery_block(self) -> str:
        """REACTIVE value recovery for the reformulate step: hand-declared dims first (a pack's deliberate
        few), then dims AUTO-derived from the failed predicate. Only fires on a blank, so a first-try hit
        pays nothing. Fail-safe and cached per dimension."""
        paths = list(self._dims)
        for p in self._auto_dims():
            if p not in paths:
                paths.append(p)
        if not paths:
            return ""
        found = self._lookup(paths)
        if found:
            self._log(f"  [recover] looked up values for {len(found)} column(s)")
        return render_values_block(found, header=RECOVERY_HEADER)

    def catalog_block(self) -> str:
        """PROACTIVE binding for the FRAMING step: profile the view's LOW-cardinality dimensions once
        (`value_catalog: {max_cardinality: N}`) and hand their real values to framing so the FIRST try
        binds the user's term to the exact stored spelling. High-cardinality dims (names, ids) are
        skipped; at most 40 dims are profiled. Off unless configured; cached for the graph's lifetime."""
        if self._catalog is not None:
            return self._catalog
        block = ""
        maxc = int(self._catalog_cfg.get("max_cardinality", 0) or 0)
        catalog = getattr(self.sql, "dimension_paths", None)
        if maxc and catalog:
            try:
                paths = list(catalog() or [])[:40]
            except Exception:
                paths = []
            found = self._lookup(paths, limit=maxc + 1)
            small = {p: v for p, v in found.items() if 0 < len(v) <= maxc}
            if small:
                self._log(f"  [catalog] {len(small)} low-cardinality dimension(s) profiled for framing")
            block = render_values_block(small, header=CATALOG_HEADER)
        self._catalog = block
        return block


class DeterministicRetriever(Retriever):
    """The engine runs every declared READ-ONLY tool once (the model never selects -> injection-safe)."""
    frames_question = True
    reformulates = True

    def __init__(self, loaded, *, zero_is_no_data=False):
        super().__init__(zero_is_no_data)
        self._loaded = loaded

    def _retrieve(self, question: str, run_id: str) -> Retrieval:
        return Retrieval.from_text(gather_context(question, self._loaded, run_id))


class AgenticRetriever(Retriever):
    """The model picks read-only tools step by step (bounded ReAct). Self-corrects across its own
    steps, so it is not reformulated by the outer loop."""
    frames_question = True
    reformulates = False

    def __init__(self, loaded, llm, act_template: str, max_steps: int, *, zero_is_no_data=False):
        super().__init__(zero_is_no_data)
        self._loaded, self._llm, self._act, self._max = loaded, llm, act_template, max_steps

    def _retrieve(self, question: str, run_id: str) -> Retrieval:
        return Retrieval.from_text(run_agentic(question, self._loaded, self._llm, self._act, run_id, self._max))


class PlannedRetriever(Retriever):
    """The model commits a validated DAG; the engine executes it and replans on failure. The planner
    owns query formulation (no outer framing) and replan is its self-correction (no outer reformulate)."""
    frames_question = False
    reformulates = False

    def __init__(self, loaded, llm, plan_tmpl: str, replan_tmpl: str, plan_skills: str, use_case: str,
                 max_plan_steps: int, max_replans: int, max_ref_chars: int, *, zero_is_no_data=False):
        super().__init__(zero_is_no_data)
        self._a = (loaded, llm, plan_tmpl, replan_tmpl, plan_skills, use_case,
                   max_plan_steps, max_replans, max_ref_chars)

    def _retrieve(self, question: str, run_id: str) -> Retrieval:
        loaded, llm, p, rp, sk, uc, mps, mr, mrc = self._a
        return Retrieval.from_text(run_planned(question, loaded, llm, p, rp, sk, run_id, uc, mps, mr, mrc))


class ToollessRetriever(Retriever):
    """`tools: []` -- a deliberately toolless agent (answers from skills alone). No retrieval, so there
    is no such thing as a blank one: never escalates."""
    def _retrieve(self, question: str, run_id: str) -> Retrieval:
        return Retrieval(text="No tools configured; answered from skills alone.", empty=False)
