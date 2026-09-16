# Architect brief — full framework review & redesign

**You are:** a principal-level AI systems architect (Claude Fable 5.1) with full authority to redesign
this repository for the best possible design.
**Mandate:** review the ENTIRE framework, then redesign it — greenfield if that's better. You may rewrite
any code, restructure the repo, replace or delete existing agents/packs, **change or discard any interface,
and replace the test suite.** Nothing here is sacred — including the current architecture, the module
boundaries, and the tests. Goal: the cleanest, most correct, most portable design informed by current best
practice — a fresh rethink, not a patch on the current shape. **§3 is not a cage: it is the list of
correctness *properties* the current system earned the hard way. Re-justify each one in your new design, or
consciously replace it with something demonstrably better — just never drop one silently and reintroduce a
bug we already fixed.**

The project: a portable LangGraph agent (generate → evaluate → refine, scored /N) built to survive a
Snowflake ↔ Databricks migration with **no retraining** — the model/platform is a swappable component
behind stable interfaces; all durable value (prompts, skills, rubrics, exemplars, evals) is git-owned
text. Runs on a MOCK provider with zero credentials; flips to real Cortex/Databricks/LiteLLM by env.

### What this is, and what already works (so you redesign the *right* thing)

A **general-purpose, portable agentic framework**: one generic engine that runs *many* agent use cases,
each defined as a small "pack" of text (a persona, skills, a rubric, exemplars, evals). A use case = a
folder, not new code. Three retrieval modes exist today — `deterministic` (engine runs read-only tools
once), `agentic` (model-driven ReAct), and `planned` (a validated multi-step DAG) — plus a swappable
LLM/SQL/tracer/memory interface layer, an evaluator that scores every answer, and a deterministic
"groundedness" guard that escalates instead of fabricating.

**Validated (offline suite green; several live on real Cortex):** the generate→evaluate→refine loop and
self-correction; grounded scoring + escalation on real SQL; deterministic + agentic + planned multi-step
(rank → demand-for-those → classify) with step-to-step chaining; the `dispatch()` tool safety boundary;
LiteLLM gateway; both hosting shells (Snowflake SPCS + Databricks) on mock; single-env-var
platform/model swap. It genuinely works across BI Q&A, non-SQL tool agents, and multi-step reasoning.

### The one gap — do NOT over-fit the redesign to it

While implementing one BI use case we hit a case the framework doesn't serve cleanly (broadly: very
open-ended / large-fan-out analytical questions, and a semantic view that lacked the grain a question
needed). **This is a single-use-case fit issue, not a framework flaw** — a general framework is not
expected to be optimal for *every* use case, and this one may simply belong elsewhere. Treat it as a
known, acceptable limitation to note in passing. **Do not reshape the architecture around it, and do not
let it dominate the redesign** — optimize for the broad set of use cases the framework is meant to serve.

---

## 1. Read first — the context pack (in THIS order; it's also the cache prefix, see §7)

Read these before writing anything. They are the source of truth and the "why."

1. `CLAUDE.md` — the operational spec + every hard-won decision (the "don't re-derive these" list). **The
   single most important file.**
2. `PROJECT_CONTEXT.md` — the narrative, goals, and decision log with reasoning.
3. `README.md` — the repo map (what's source vs scaffolding) and the tier model.
4. `docs/concepts/*.md` — the mechanisms in depth: `end-to-end-flow`, `the-refine-loop`,
   `retries-explained`, `tools-and-agents`, `multi-step-planning`, `instructions`, `ai-gateway`,
   `use-case-pack-anatomy`, `access-control`, `evals-and-the-learning-flywheel`.
5. `docs/testing/*.md` — live test reports (real behavior observed in the field).
6. The code, in dependency order: `engine/graph.py` → `engine/tools/` (`base`, `dispatch`, `registry`,
   `orchestrator`, `agentic`, `planned`, adapters) → `engine/llm_client.py` → `engine/sql_tool.py` →
   `engine/tracing.py` → `engine/memory.py` → `engine/platform_*/` → `shared/` → `usecases/` → `tests/`.
7. `git log` — the decision trail. Many oddities are hard-won fixes; **read the commit before "simplifying"
   something that looks over-complex.**
8. `tests/` — a **behavior reference**, not a spec to preserve (334 tests). Mine them for the edge cases
   and guarantees they encode (that's their value), then write whatever new suite your design deserves.
   The bar is "every §3 property is covered," not "these exact tests still pass."

---

## 2. Deliverables & method (do NOT big-bang rewrite)

Work in this order; get sign-off at each gate before the next.

1. **Assessment** — a **holistic** written critique of the whole framework: what's excellent (keep), what's
   accidental complexity, the real architectural smells, and the bugs/debt *you* judge to matter, ranked by
   impact. Derive this yourself from the code, tests, and git history.
2. **Target design** — one document: the redesigned architecture end-to-end (modules, interfaces, data
   flow, the loop, tools, memory, observability, security, deployment, repo layout), with a clear
   **before → after** and the rationale for each change. Call out every interface break.
3. **Migration plan** — phased, each phase independently shippable and test-green, with a rollback point.
   Sequence risky/foundational changes first behind the existing tests.
4. **Implement** — phase by phase. TDD: write/adjust tests first, keep `pytest -q` green at every phase,
   never leave the tree broken. Update the docs that describe what you changed.

Rules of engagement:
- **Challenge assumptions, including mine.** If a "hard-won decision" is actually wrong, say so with the
  evidence — don't silently preserve or silently discard it.
- **Don't over-engineer.** Simplicity and correctness beat cleverness. Every added abstraction must earn
  its keep; prefer deleting code to adding it.
- **Every non-obvious choice gets a one-line "why"** in a decision log, so the next architect doesn't
  re-litigate it (that's exactly what `CLAUDE.md`'s decision list is for — extend it).
- **Report honestly:** if a test fails, show it; if a design has a residual risk, name it; if you didn't
  verify something (e.g. a live Cortex path), say so.
- **The human runs all git operations and any live/credentialed run.** You work on the tree and hand off
  commands.

---

## 3. Correctness properties — re-justify or replace, never silently drop

These are not design mandates and not the current mechanisms — they are the *properties* that made the
system correct, each learned from a real failure. In your fresh design, for each one: keep it (new
mechanism is fine), or replace it with something provably better and say why. The only hard rule: don't
lose one by accident and reintroduce a bug we already fixed. A clean rewrite that silently regresses one
of these is a failure, however elegant.

- **Portability is the point.** Generic core imports no vendor SDK except inside its one adapter class.
  Model/platform swap by config, no code change. All tuning stays git-owned text.
- **Groundedness is deterministic, never the judge's job.** A blank/empty retrieval must never be scored
  as a good answer — it escalates (`no_data`/`out_of_scope`, judge skipped, `score=None`). See the
  grounding + reformulate-guard machinery; keep the guarantee.
- **Judge independence & grounded scoring.** The evaluator scores against retrieved evidence, is a
  separately selectable model, and never rewrites the question/SQL. The verdict is validated (denominator
  check, rounding, bounded retries, fail-closed to 0).
- **`dispatch()` is the one tool trust boundary** — validate → approval-gate (writes need explicit
  approval; never auto-run) → timeout → redact → bound → never raises. Every tool call goes through it.
  `read_only` is required + authoritative. HTTP tool: allowlist + SSRF re-validation + bounded.
- **Untrusted data stays untrusted.** Tool output, DB values, and step results are data, never
  instructions (injection banners; bounded hand-offs). Secrets are redacted from logs/errors.
- **`.env` is a dummy test file** read only by runner/test scripts — never by `engine/`. `engine/` reads
  process env for deployment config, never the `.env` file.
- **Observability is out of the loop** — nodes stay pure; tracing is applied from outside and is
  fail-safe with zero overhead when off.
- **Deterministic, creds-free tests** — the whole suite runs on the mock with no network; a scripted LLM
  double drives agentic/planned paths.

Changing one is fine — it just has to be a **conscious, justified decision in the design doc**, not a
side effect of a refactor.

---

## 4. You are free to redesign (non-exhaustive)

Repo layout & packaging (a `src/` layout, real `pyproject.toml`, console entry points); the module
boundaries and interfaces; the State model and graph topology; the tool/mode taxonomy
(`deterministic|agentic|planned`) — unify or re-cut it if better; memory (currently deferred/episodic);
the config schema; how packs are authored; error/latency handling; the example packs (rewrite or replace
them). If a cleaner primitive collapses several current mechanisms (reformulate / refine / re-review /
replan), propose it.

---

## 5. Context you can't get from the repo

- **Domain:** enterprise BI/analytics agents over Snowflake Cortex Analyst semantic views today
  (`INVENTORY_ANALYTICS`), Databricks/Genie later. Users are analysts; answers feed human decisions, so a
  wrong-but-confident answer is worse than an honest "can't answer."
- **The migration constraint is real** — Snowflake↔Databricks is an actual planned move; portability is
  not hypothetical.
- **Live behavior notes** (see `docs/testing/`), all tied to the single ill-fitting use case in §0 —
  context, not design drivers: opus-class synthesis is slow on very broad inputs; Cortex Analyst
  sometimes returns empty for over-combined questions; the `INVENTORY_ANALYTICS` view lacks the grain a
  few questions needed (unanswerable by *data*, not code). Note them; don't design around them.
- **Environment:** Windows; Python 3.14; tests run green offline; the user handles git and live runs.

---

## 6. How to get the best out of you (working style)

- **Think before you build.** Spend budget on the assessment and target design; implementation is the
  cheap part once the design is right.
- **Prefer one coherent design over a menu.** Give a recommendation with the trade-off, not five options.
- **Small, verifiable steps.** Each change compiles, tests pass, and is explainable in one sentence.
- **When two designs are close, pick the one a non-expert pack author can use** — authoring a new agent
  should stay "add a folder of text files."
- **Leave the campsite cleaner:** delete dead code, fix docs you touch, and record decisions.

---

## 7. Caching / cost strategy (make the long redesign cheap)

The redesign is a long, multi-turn session over a large, mostly-stable context. Exploit prompt caching:

- **Stable prefix, volatile suffix.** Put the big, unchanging context at the FRONT and keep it byte-stable
  across turns so it stays cached: (a) this brief, (b) `CLAUDE.md` + `PROJECT_CONTEXT.md`, (c) the concept
  docs, (d) the current source tree. Put the *current task/question* at the END. Don't reorder or reword
  the prefix mid-session — any edit above a change point invalidates the cache from there down.
- **Load the whole repo once**, early, as that cached prefix — re-reading files each turn is the main
  waste. Anthropic caches have a ~1-hour TTL on this session class, so within a working block the cached
  context is reused for free; a `cache_control` breakpoint after the context pack is ideal.
- **Freeze the invariants (§3) and the read-order (§1) as part of the prefix** so they're always in
  context without re-sending.
- **Snapshot before each phase.** Commit (human) at every green gate so the diff per phase is small and
  the context needed to review it is small.
- **Keep the decision log append-only** — new decisions go at the end, never rewriting earlier ones, so
  the cached prefix stays valid.
- **Don't paste giant tool outputs back into the prompt** — reference files by path; let the model
  re-read only what changed.

---

## 8. Definition of done

- A written assessment, a target-design doc, and a phased migration plan — all reviewed and approved
  before large-scale edits.
- Tests green at every phase (your new suite); no phase leaves the tree broken.
- Every §3 property either covered by the new design (a test or a documented argument) or consciously
  replaced with a justified better alternative.
- Docs updated for what changed; `CLAUDE.md` decision list extended with the new "why"s.
- The one-line test still true: **a new agent = a folder of text files; a platform swap = one env var.**

---

## First task

Read the context pack (§1), then produce **§2.1 — a holistic assessment of the entire framework**, ranked
by impact and derived from the code/tests/git history yourself. Do not modify code until the assessment
and target design are approved.
