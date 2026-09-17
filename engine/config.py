"""
ENGINE / config -- the TYPED pack configuration. One pydantic model is the single source of truth for
every knob a pack may set: its default, its bounds, and its meaning. `extra="forbid"` means a typo in a
pack's config.yaml (`max_plan_step:`) is an ERROR at build time, not a silent no-op -- the untyped-dict
version of this let typos through.

Security-sensitive booleans are STRICT: YAML's quoted "false" must not become True (`bool("false")`),
so only real booleans or the tokens true/false/yes/no/on/off/1/0 are accepted.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, StrictInt, field_validator, model_validator

from engine.tools.base import strict_bool

ToolMode = Literal["deterministic", "agentic", "planned"]


class PackConfig(BaseModel):
    """Validated, bounded pack config. Build with `PackConfig.from_dict(load_config(pack))`."""
    model_config = ConfigDict(extra="forbid")

    # identity / manifest
    name: str = ""
    description: str = ""
    sample_task: str = ""
    sample_questions: list[str] | None = None

    # scoring / loop bounds
    max_score: StrictInt = Field(18, ge=1)            # the rubric's authored total (denominator)
    pass_score: StrictInt | None = Field(None, ge=1)  # stop bar; `threshold` is the back-compat alias
    threshold: StrictInt | None = Field(None, ge=1)
    max_iters: StrictInt = Field(4, ge=0, le=10)      # <=10 keeps under LangGraph's recursion limit
    eval_retries: StrictInt = Field(1, ge=0)          # judge re-asks on an unparseable verdict
    max_stall: StrictInt = Field(2, ge=0)             # no-progress stop (0 = off)

    # retrieval strategy
    tool_mode: ToolMode = "deterministic"
    default_sql_tool: str = "mock"
    tools: list[dict[str, Any]] | None = None         # absent -> legacy single-SQL; [] -> toolless
    max_tool_steps: StrictInt = Field(4, ge=0, le=10)         # agentic
    max_plan_steps: StrictInt = Field(8, ge=1, le=20)         # planned
    max_replans: StrictInt = Field(2, ge=0, le=5)             # planned
    max_ref_chars: StrictInt = Field(2000, ge=200, le=8000)   # planned {{sN}} hand-off bound
    max_data_retries: StrictInt = Field(1, ge=0, le=5)        # reformulate-and-retry budget on a blank
    zero_is_no_data: bool = False                      # a lone all-zero/NULL row counts as blank
    frame_query: bool = False                          # skill-informed query formulation before retrieval
    frame_skills: list[str] | str | None = None
    plan_skills: list[str] | str | None = None
    recover_value_dims: list[str] | None = None        # hand-declared 'TABLE.DIM' paths (override/priority);
                                                       #   auto-derivation from the failed predicate needs no list
    value_catalog: dict[str, Any] | None = None        # {max_cardinality: N}: profile low-card dims into framing

    # prompt composition
    exclude_shared_skills: list[str] | None = None
    exclude_shared_instructions: list[str] | None = None
    allow_runtime_instructions: bool = False           # per-request instructions trust gate
    models: dict[str, Any] | None = None               # pack-level worker/evaluator profiles (env wins)

    # seams (opt-in; default off = zero behavior change)
    output_schema: dict[str, Any] | None = None        # declared structured output (JSON Schema-ish)
    judge_verify_tool: str | None = None               # a read-only tool label the judge may spot-check with

    _raw: dict = PrivateAttr(default_factory=dict)
    _tools_declared: bool = PrivateAttr(default=False)

    # --- validators -------------------------------------------------------------------------------
    @model_validator(mode="before")
    @classmethod
    def _strict_bools(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data = dict(data)
            for k in ("zero_is_no_data", "frame_query", "allow_runtime_instructions"):
                if k in data:
                    data[k] = strict_bool(data[k], False)   # raises on a typo like "maybe"
        return data

    @field_validator("tool_mode", mode="before")
    @classmethod
    def _norm_mode(cls, v):
        return str(v).strip().lower() if v is not None else "deterministic"

    @field_validator("recover_value_dims", mode="before")
    @classmethod
    def _dims(cls, v):
        if not v:
            return None
        if not isinstance(v, list) or not all(isinstance(d, str) and d.strip() for d in v):
            raise ValueError("recover_value_dims must be a list of 'TABLE.DIM' dimension paths")
        return v

    @field_validator("value_catalog", mode="before")
    @classmethod
    def _catalog(cls, v):
        if v is None:
            return None
        if not isinstance(v, dict):
            raise ValueError("value_catalog must be a mapping like {max_cardinality: 25}")
        mc = v.get("max_cardinality", 25)
        if type(mc) is not int or not 1 <= mc <= 200:
            raise ValueError("value_catalog.max_cardinality must be an integer in 1..200")
        return {"max_cardinality": mc}

    @model_validator(mode="after")
    def _cross_checks(self):
        if self.pass_score is None:                    # `threshold` is the alias; default = max_score
            self.pass_score = self.threshold if self.threshold is not None else self.max_score
        if self.pass_score > self.max_score:
            raise ValueError("pass_score must be <= max_score")
        return self

    # --- construction -----------------------------------------------------------------------------
    @classmethod
    def from_dict(cls, raw: dict) -> "PackConfig":
        """Validate a merged pack dict (see engine.packs.load_config). Keeps the raw dict so the tool
        loader can distinguish an ABSENT `tools:` key (legacy SQL) from `tools: []` (toolless)."""
        cfg = cls.model_validate(raw)
        cfg._raw = dict(raw)
        cfg._tools_declared = "tools" in raw
        return cfg

    @property
    def raw(self) -> dict:
        return self._raw

    @property
    def tools_declared(self) -> bool:
        return self._tools_declared
