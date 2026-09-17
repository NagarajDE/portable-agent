# Architect assessment — portable-agent (brief §2.1)

**Status:** approved and **IMPLEMENTED** — the resulting design, before→after per finding, and the
decision log are in [`target-design.md`](target-design.md). Findings B.1–B.7, B.11, B.12 addressed;
B.9 (in-node trace emit) and B.10 (packaging) deliberately left as follow-ups.
**Basis:** full read of `CLAUDE.md`, `PROJECT_CONTEXT.md`, README, concept docs, all of `engine/`,
`shared/`, `usecases/`, the 334-test suite, and the git history (34 commits). Line refs are current.
**Verdict in one line:** the *product idea* and the *safety/correctness guarantees* are right and worth
keeping; the *shape of the core* has accreted into one god-module with five retrieval paths, three retry
regimes, and string-typed contracts — that shape, not any single feature, is what to redesign.

---

## A. What is genuinely excellent — keep the idea, keep the guarantee

| Keep | Why it's right |
|---|---|
| **Three-tier ownership** (`engine/` generic · `shared/` conventions · `usecases/` packs) and "a new agent = a folder of text files" | This *is* the product. It's what makes tuning portable and non-engineers productive. Every redesign must preserve it. |
| **`dispatch()` as a total, never-raises trust boundary** (`engine/tools/dispatch.py:93`) — validate → approval gate → timeout → normalize → redact → bound → one log line | Best-designed code in the repo. The right answer to "how do tools stay safe" — the redesign should make it the *only* way anything external is touched (see B.3). |
| **Groundedness is deterministic; escalate, never fabricate** | Correct principle, hard-won (a no-data answer once scored 15/18). Keep the *guarantee*; the *encoding* is wrong (B.2). |
| **Refine-from-best, judge independence, validated `Verdict`** (JSON-authoritative, denominator check, fail-closed to 0 — `graph.py:301`) | Each closes a real scoring bug; scoring decides what a human sees. Keep all three. |
| **Fail-safe, zero-overhead observability & memory; lazy vendor imports** | Right seam pattern; never breaks an answer. |
| **Creds-free deterministic test suite with scripted doubles** (334 tests) | A real asset. Keep the *cases*; restructure the *fixtures* (B.6). |
| **Docs + decision log** (`PROJECT_CONTEXT.md` §6/§12, concept docs) | Unusually good "why" trail. The redesign should extend it, not replace it. |

---

## B. Findings — ranked by impact on the design

### B.1 — `build_graph` is a ~370-line closure factory; `generate` is the accretion point · **Highest**
`build_graph` (`engine/graph.py:478–847`) reads ~15 config knobs, constructs every dependency, loads
prompts/skills/exemplars/instructions, and then defines the nodes as **inner closures** (`_retrieve`,
`_recover_values_block`, `_frame_question`, `generate`, `evaluate`, `refine`, `keep_going`,
`after_generate`, `after_refine`). `generate` alone is ~95 lines doing framing → retrieval →
reformulate-retry (three guards, two escalation reasons) → zero-row detection → grounding → escalation.
Every feature this year (framing, `known_values`, reformulate, planned) landed *inside* it. `graph.py`
(854 LOC) also owns config loading, skill/exemplar/instruction loading, `fill`, verdict parsing, the
lexical drift guards, and `State` — a god-module.
**Consequence:** nothing in the loop is unit-testable without building a whole graph against a real pack
dir and monkey-patching `load_config` (~30 tests do exactly this); each new capability compounds the
risk. **This is the load-bearing smell** — most of B.2–B.7 are its symptoms.

### B.2 — Retrieval is five code paths + three self-correction regimes with per-mode exclusion booleans · **High**
`_retrieve` branches on legacy-SQL / agentic / planned / toolless / deterministic. Self-correction is
*three different mechanisms*: reformulate-retry (SQL & deterministic only), replan (planned only), and
the ReAct loop (agentic only), gated by hand-maintained booleans (`can_retry`, `frames_retrieval`) that
must be kept in sync by hand — the exact bug class we hit (planned was silently excluded/included by
edits to those lines). **There should be one `Retriever` strategy interface with one blank-handling
contract**, and self-correction should be a property of the strategy, not a set of special cases in the
node.

### B.3 — Two parallel SQL paths, and the legacy one bypasses the trust boundary · **High**
Packs without `tools:` call `sql.ask()` directly (`graph.py` `use_sql`); packs with `tools:[{type:sql}]`
go through `SqlBridgeTool` → `dispatch()`. Same `get_sql_tool` underneath, **two behaviors**: the bridge
path gets timeout, redaction, output bounds, and normalized errors; the legacy path gets none of them
(only the adapter's own SQL timeout). Kept for "zero behavior change," it's now a real asymmetry in the
safety story. **SQL should always be a tool** (one path, dispatch-guarded), with legacy pack config
auto-mapped.

### B.4 — Groundedness is a magic string smuggled through `str` contracts · **High**
`SQLTool.ask() -> str` and `ToolResult.output: str` force "no data" to be the sentinel
`"__PA_NO_DATA__"` (`sql_tool.py`). That in turn forced `is_no_data`/`data_hint` sniffing everywhere,
`SqlBridgeTool` returning a sentinel as `ok=True`, a **latent bug in `gather_context`** (counts a
sentinel string as usable because it checks `.strip()`, not `is_no_data`), and `run_planned` needing
to special-case it. A typed retrieval result — `Retrieval(rows|text, empty: bool, hint: str)` —
deletes the whole sentinel class and the latent bug. Direct consequence of B.2's string-typed
interfaces.

### B.5 — Config is an untyped dict with hand-rolled validation · **High (low risk to fix)**
`load_config` returns a `dict`; `build_graph` pulls ~15 keys with `.get(k, default)`, validates them in
a bespoke loop plus a pile of `if x > N: raise`. `strict_bool` exists only because `bool("false")` once
bit us. **A typo in a pack key is silently ignored** (`max_plan_step:` is a no-op). A pydantic
`PackConfig` (`extra="forbid"`, bounds as field constraints) catches typos, centralizes every default
and cap, self-documents every knob, and removes ~60 lines from `build_graph`. Pydantic is already a
dependency (Verdict, tool inputs).

### B.6 — Environment/settings are read ad hoc in ~10 modules · **Medium-High**
`os.getenv` is called directly in `llm_client`, `sql_tool`, `dispatch`, `tracing`, `memory`,
`orchestrator`, `agentic`, `planned`, and both platform shells. `conftest.py` must enumerate **40 env
vars** to isolate tests — a symptom. One `Settings` object built once at the composition root and passed
down makes behavior explicit and testable without env mutation. (This is consistent with the `.env`
rule: the engine *may* read process env — it should do so in one place.)

### B.7 — Test fixtures are duplicated and coupled to the god-module · **Medium**
`_Cap`/`_JudgeSpy`/`_Sql`/`ScriptedLLM` are re-declared across four test files; most graph tests
exist only as "monkeypatch `load_config`, build a graph on `dq_qals`, invoke." Symptom of B.1/B.5. Fix:
one `tests/doubles.py`; node-level tests once nodes are real objects.

### B.8 — The lexical drift guards are the most "clever" code and live in the wrong place · **Medium**
`_stem`/`_content_words`/`_pinned_codes`/`_keeps_subject`/`_keeps_pinned` (`graph.py:375–456`) are a
hand-rolled bag-of-words stemmer + stopword set. They work (tests cover the cases) but are brittle by
construction — documented gaps: dimension-preserving swaps, `FY26`→"fiscal 2026" false-drift, irregular
plurals. Move to a `guards.py`; longer-term, consider a *structural* guard (compare extracted
entities/filters) over a lexical one. Not urgent; flagged because clever code is where regressions hide.

### B.9 — `run_planned` emits trace events from inside a node · **Low**
Breaks the stated rule "nodes are pure; tracing is applied from outside" (`CLAUDE.md`). I introduced
it. Cleaner: `run_planned` returns a structured step trace; the node wrapper emits. Small, but rules
erode one exception at a time.

### B.10 — Packaging & layout · **Medium (own phase)**
No `pyproject.toml`; three top-level importable dirs; runners at repo root relying on cwd on `sys.path`.
Deployment ships directories via `code_paths`. A `src/portable_agent/` package + `pyproject` + console
entry points is the modern standard and makes `engine` importable anywhere — but it touches the
Dockerfile, `spec.yaml`, and `code_paths`, so it's its own phase, not a side effect.

### B.11 — State is a flat per-call `TypedDict` with no session seam · **Medium (design the seam, don't build recall)**
`initial_state` mints a fresh run per call; memory is write-only capture; multi-turn is deliberately
parked. That's a fine scope decision — but the new core should carry a `thread_id`/session seam now
(cheap) so conversational use cases don't require a State rewrite later.

### B.12 — Capability gaps (corroborated by `docs/design/framework-gaps.md`) · **context, not the agenda**
The register there is a fair holistic map across archetypes. Two of its items change the **loop
contract** and should be *designed into* the new core rather than bolted on later: **a judge that can
verify against data** (GAP-1) and **a declarable structured output** (GAP-4). The rest (MCP finish,
source breadth, HITL, batch, streaming, memory recall) are additive behind the same seams. I'm noting
this so the core design leaves room; I am not designing around any one use case.

---

## C. What the redesign should change the *shape* of (preview — full design is the next gate)

1. **Composition root, not closure factory.** A `PackConfig` (pydantic) + a `Settings` object are built
   once; nodes become small, injectable, unit-testable objects/functions. `build_graph` shrinks to wiring.
2. **One `Retriever` abstraction** (deterministic / agentic / planned as strategies) returning a **typed
   `Retrieval`** (no sentinel), with self-correction as a strategy property and one grounding contract.
3. **SQL is always a tool** through `dispatch()`; the legacy direct path goes away (config auto-mapped).
4. **Typed contracts end-to-end** — `Retrieval`, `Verdict`, and a declarable pack **output schema**
   (leaves room for GAP-4) — with the judge able to take a *verification* tool (GAP-1) as an opt-in.
5. **Guards, verdict parsing, config, and State** each get their own module; `graph.py` becomes the
   loop and nothing else.
6. **Packaging as a real Python package** in a separate, later phase.

Everything in §A survives intact; everything in §B is addressed by 1–6. The migration will be phased
and test-green at each gate, per the brief.

---

## D. Risks in the redesign itself (so we plan for them)

- **Silently regressing a §3 property while "cleaning up"** — the biggest risk. Mitigation: port every
  test *case* first (the suite is the behavior reference), then refactor under them.
- **Deployment packaging** (`code_paths`, Dockerfile, `spec.yaml`) is brittle to a layout change —
  isolate it as its own phase with the mock-first deploy check.
- **The live Cortex paths are untestable offline** — they've been validated live; any adapter change
  needs a human live run before it's called done.

**Recommendation:** approve this assessment, then I produce the target design (§2.2) with a
before→after per change and a phased, test-green migration plan (§2.3).
