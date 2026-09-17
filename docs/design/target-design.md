# Target design — the redesigned core (brief §2.2/§2.3, IMPLEMENTED)

**Status:** built and green — **358 tests** (334 behavior-reference cases preserved + 24 new), every
retrieval mode smoke-tested on the mock. Deployment layout (`engine/`+`shared/`+`usecases/`, the two
platform shells, `code_paths`, Dockerfile, `spec.yaml`) is **unchanged**. Packs are unchanged: every
existing `config.yaml`, prompt, skill, and fixture works as-is.

## What changed (before → after)

| Concern | Before | After |
|---|---|---|
| **Loop composition** (B.1) | `build_graph` = ~370-line closure factory; nodes were inner closures; `graph.py` 854 LOC owned config, loaders, templating, verdict, guards, State, loop | `graph.py` is a **composition root** (~160 LOC): validate config → build dependencies → wire nodes. Nodes are plain functions of an explicit `Runtime` in **`engine/nodes.py`** — unit-testable with doubles. |
| **Retrieval** (B.2) | 5 branches in `_retrieve` + 3 self-correction regimes gated by hand-synced booleans (`can_retry`, `frames_retrieval`) | **`engine/retrieval.py`**: one `Retriever` strategy interface (`SqlRetriever`, `DeterministicRetriever`, `AgenticRetriever`, `PlannedRetriever`, `ToollessRetriever`) returning a typed **`Retrieval`**. Each strategy declares `frames_question` / `reformulates`; the loop never branches on a mode. |
| **Groundedness encoding** (B.4) | `"__PA_NO_DATA__"` sentinel sniffed in graph, planned, orchestrator; `gather_context` had a latent bug (counted a sentinel as usable) | **`Retrieval(text, empty, hint)`** is the contract. The sentinel survives only as the *wire format* of text-protocol adapters and is interpreted in exactly ONE place (`Retrieval.from_text`). `gather_context` fixed. |
| **SQL trust boundary** (B.3) | Legacy packs called `sql.ask()` directly — **no timeout / redaction / output bound**; tools-packs went through `dispatch()` | **SQL is always a tool.** `SqlRetriever` wraps any `SQLTool` (`_SqlAsTool`) and runs it through `dispatch()`. One path, one boundary. A hard failure on the *first* retrieval is still fatal (re-raised); a retry's failure is still non-fatal. |
| **Config** (B.5) | Untyped dict; ~15 `cfg.get`s; hand-rolled bounds loop; a typo (`max_plan_step:`) silently ignored | **`engine/config.py: PackConfig`** (pydantic, `extra="forbid"`, `StrictInt` bounds, strict bools, `threshold`→`pass_score` alias). A typo or bad bound **fails at build time**. Single source of truth for every knob, its default and its cap. |
| **Env reads** (B.6) | `os.getenv` in ~10 modules | **`engine/settings.py`** for the loop's deployment knobs (read once per build, passed down). Vendor adapters keep their own credential env reads at the adapter edge (platform-injected deployment config — by design). |
| **Verdict / guards / loaders** | all in `graph.py` | **`engine/verdict.py`**, **`engine/guards.py`** (with a single `rewrite_drift()` classifier), **`engine/packs.py`** (loaders take explicit roots — no hidden globals). `graph.py` re-exports the public names, so callers and tests are unchanged. |
| **Test doubles** (B.7) | re-declared in 4 files | **`tests/doubles.py`** (`Cap`, `Judge`, `Sql`, `ScriptedLLM`); new tests use it. (Older files keep their local copies — migrating them is churn with no behavior value; left as a follow-up.) |
| **Loop-contract seams** (B.12 · GAP-1/GAP-4) | none | `output_schema:` — a declared structured-output contract the loop **enforces** (violation → score 0 with the reason → refine fixes the *shape*; the judge is never asked to grade a malformed object). `judge_verify_tool:` — the judge may spot-check with ONE declared **read-only** tool whose output is appended to the rubric evidence. Both opt-in, default off (zero behavior change). |
| **Session seam** (B.11) | none | `thread_id` on `State` / `initial_state(..., thread_id=)`; carried, not yet used — multi-turn recall stays a future capability behind `MemoryStore`. |

## What did NOT change (deliberately)

- **Every §3 property** (grounding-is-deterministic, escalate-never-fabricate, judge independence,
  validated verdict, refine-from-best, failed-refine-non-fatal, no-progress stop, dispatch as the one
  boundary, `.env` never read by `engine/`, fail-safe tracing, creds-free tests) — re-proven by the
  preserved 334 cases plus the new ones.
- The pack-author contract ("a new agent = a folder of text files") and the platform-swap contract
  ("one env var").
- Repo layout / packaging (B.10): a `src/` layout + `pyproject.toml` is a worthwhile **separate phase**
  because it touches the Dockerfile, `spec.yaml`, and MLflow `code_paths`; not bundled here.
- `run_planned` still emits its per-step trace events from inside the node (B.9) — small, flagged, left.

## Decision log (append to CLAUDE.md's list)

20. **Retrieval is one typed abstraction, not a mode switch.** Strategies own their self-correction
    (`reformulates`) and framing (`frames_question`) flags; the loop asks, never branches. Adding a
    retrieval kind = one class, zero loop edits.
21. **The NO_DATA sentinel is a wire format, not an engine concept.** Interpreted only in
    `Retrieval.from_text`. No other module may sniff it.
22. **SQL always goes through `dispatch()`.** The "zero behavior change" legacy bypass was a safety
    asymmetry (no timeout/redaction/bounds); the bound output is a small, correct behavior change.
23. **Pack config is typed and closed.** Unknown keys are errors. The cost of a strict schema is paid once
    (list the keys); the cost of a lenient one is paid forever (silent typos).
24. **Loop-contract seams are designed in, off by default.** `output_schema` and `judge_verify_tool`
    exist so report/JSON agents and data-verifying judges don't require a core rewrite later.

## Migration record

Built as phases, each test-green: (1) extract `packs`/`verdict`/`guards` with re-exports · (2) typed
`PackConfig` · (3) typed `Retrieval` + strategies, SQL via dispatch, `gather_context` fix · (4) `nodes.py`
+ `Runtime`, `graph.py` → composition root · (5) seams (`output_schema`, `judge_verify_tool`,
`thread_id`), `settings.py`, `tests/doubles.py`, 24 new tests · (6) docs. One test seam repointed
(`gather_context` spy now patches `engine.retrieval`, its new home) — same behavior under test.
