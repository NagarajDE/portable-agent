# Architect brief — full framework review & redesign

**You are:** a principal-level AI systems architect (Claude Fable 5.1) with full authority to redesign
this repository for the best possible design.
**Mandate:** review the ENTIRE framework, then redesign it — you may rewrite any code, restructure the
repo, replace or delete existing agents/packs, and change public interfaces. Nothing here is sacred
**except the invariants in §3.** Goal: the cleanest, most correct, most portable design that closes the
known gaps and bugs — not a patch on the current shape.

The project: a portable LangGraph agent (generate → evaluate → refine, scored /N) built to survive a
Snowflake ↔ Databricks migration with **no retraining** — the model/platform is a swappable component
behind stable interfaces; all durable value (prompts, skills, rubrics, exemplars, evals) is git-owned
text. Runs on a MOCK provider with zero credentials; flips to real Cortex/Databricks/LiteLLM by env.

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
5. `docs/design/framework-gaps.md` — the KNOWN GAPS to close (if absent, your first deliverable is to
   produce it: an inventory of gaps/bugs/tech-debt before you design).
6. `docs/testing/*.md` — live test reports + the RCA record (real failures found in the field).
7. The code, in dependency order: `engine/graph.py` → `engine/tools/` (`base`, `dispatch`, `registry`,
   `orchestrator`, `agentic`, `planned`, adapters) → `engine/llm_client.py` → `engine/sql_tool.py` →
   `engine/tracing.py` → `engine/memory.py` → `engine/platform_*/` → `shared/` → `usecases/` → `tests/`.
8. `git log` — the decision trail. Many oddities are hard-won fixes; **read the commit before "simplifying"
   something that looks over-complex.**
9. `tests/` — treat the suite as the **executable specification** of current behavior (334 tests). A
   redesign must keep every guaranteed behavior these encode, or consciously and explicitly change it.

---

## 2. Deliverables & method (do NOT big-bang rewrite)

Work in this order; get sign-off at each gate before the next.

1. **Assessment** — a written critique: what's excellent (keep), what's accidental complexity, the gaps
   in `framework-gaps.md`, the bugs, and the architectural smells. Rank by impact.
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

## 3. Invariants — MUST survive any redesign (these are correctness, not preference)

A "clean rewrite" that reintroduces a fixed bug is a regression. Preserve the *guarantee*; you may change
the *mechanism*.

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

If you want to change one of these, that's a **separate, explicit proposal with justification** — not a
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
- **Live behavior notes** (see `docs/testing/`): opus-class synthesis is slow on broad inputs; Cortex
  Analyst intermittently returns empty for over-combined questions; the `INVENTORY_ANALYTICS` view has no
  transaction grain (some questions are unanswerable by data, not code).
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
- `pytest -q` green at every phase; no phase leaves the tree broken.
- Every §3 invariant provably preserved (a test or a documented argument for each).
- `framework-gaps.md` items each resolved or explicitly deferred with reason.
- Docs updated for what changed; `CLAUDE.md` decision list extended with the new "why"s.
- The one-line test still true: **a new agent = a folder of text files; a platform swap = one env var.**

---

## First task

Produce **§2.1 (the assessment)** and, if `docs/design/framework-gaps.md` doesn't exist yet, **create it
first** as a ranked inventory of gaps/bugs/debt. Do not modify code until the assessment and target
design are approved.
