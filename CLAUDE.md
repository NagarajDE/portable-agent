# Portable Agent — Project Memory for Claude Code

Read this before making changes. It captures the architecture decisions and the
reasoning behind them, so you don't re-litigate them per session.

> For the full narrative — the goal, why the project exists, the concepts we
> clarified, and the decision log with reasoning — see **`PROJECT_CONTEXT.md`**.
> This file is the operational spec; that one is the story and the "why".
> New to the terms (interface, LLMClient, engine, pack, verdict…)? See **`GLOSSARY.md`**.

## What this is

A LangGraph agent (generate → evaluate → refine, scored against an /N rubric)
built to survive a Snowflake ↔ Databricks migration without retraining or
rediscovering tuning. It runs on a MOCK provider today (zero credentials) and
swaps to real Cortex/Databricks/Anthropic via one env var each.

## The core principle (don't violate this)

> The model/platform is a replaceable component behind a stable interface.
> Everything valuable references the interface — never a vendor SDK directly.

Concretely: `engine/` never imports `anthropic`, `snowflake`, or `databricks`
directly except inside the one adapter class built for that vendor. If you're
about to add vendor-specific logic to `engine/graph.py`, stop — it belongs in
an adapter (`engine/llm_client.py` / `engine/sql_tool.py`) or a use-case pack.

## Three-tier architecture

```
engine/     GENERIC. shared code. touch rarely.
  graph.py             the loop: generate→evaluate→refine, composes shared+pack
  llm_client.py         LLMClient interface + mock|anthropic|cortex|databricks
  sql_tool.py           SQLTool  interface + mock|cortex-analyst|genie
  tracing.py            Tracer      interface + stdout|otel|mlflow|eventtable|none
  memory.py             MemoryStore interface + none|mock|sqlite|snowflake (episodic+feedback)
  platform_databricks/agent.py   the ONLY Databricks-specific file (~25 lines)
  platform_snowflake/             the ONLY Snowflake-specific files (SPCS)
    agent.py           FastAPI shell: POST /invoke, GET /healthz
    Dockerfile         builds from repo root, ships engine+shared+usecases
    spec.yaml          SPCS service spec (image, env, endpoint)

shared/     COMMON conventions. inherited by every pack unless overridden.
  prompts/rubric.md      base /18 rubric        (pack overrides if domain needs it)
  prompts/refine.md      base self-correction
  skills/reporting.md    house reporting format  (every agent gets it)
  skills/sql_safety.md   SELECT-only safety      (every agent gets it)
  config.yaml            default threshold/max_iters/tool

usecases/<name>/     ONE folder = ONE agent. this is what you add per use case.
  config.yaml          inherits: [shared] + name + sample_task  (a few lines)
  prompts/generate.md  the persona — the one file that's truly per-agent
  prompts/rubric.md    ONLY if this domain needs different scoring than shared
  skills/*.md          ONLY domain-specific how-to (not reporting/safety — inherited)
  exemplars/*.yaml      verified Q→SQL pairs (your portable few-shot / seed)
  evals/golden_set.yaml your portable eval questions
  fixtures.py           MOCK-only demo data; ignored once SQL_TOOL/WORKER_PROVIDER are real
```

### Composition rules (implemented in `engine/graph.py`)
- **skills**: `shared/skills/*.md` + `pack/skills/*.md` → CONCATENATED
- **prompts**: `pack/prompts/<f>` if it exists, ELSE `shared/prompts/<f>` → OVERRIDE
- **config**: `shared/config.yaml` merged with `pack/config.yaml` (pack wins) via
  `inherits: [shared]`

### Adding a new agent = adding a folder, never touching `engine/` or `shared/`
```
usecases/my_new_agent/
  config.yaml          inherits: [shared], name, sample_task
  prompts/generate.md  persona (+ {skills}{exemplars}{data} placeholders)
  exemplars/  evals/    your tuning
  # rubric.md / refine.md only if overriding shared
```

## Existing use-case packs (reference examples)

| pack | mode | rubric axes |
|---|---|---|
| `dq_qals` | is the data right? | rule correctness · evidence & quantification · actionability |
| `kpi_analytics` | what does the metric say? | metric-definition correctness · time grain & framing · clarity |
| `anomaly_rca` | why did it change? | hypothesis quality · evidence & isolation · actionable conclusion |
| `parity_hana_snowflake` | cross-engine parity | overrides the shared rubric (demonstrates override, not inherit) |

## Hard-won decisions from design discussion (don't re-derive these)

1. **"Fine-tuning" these agents ≠ training weights.** Improvement happens via
   config (instructions, verified queries, rubric) — the base LLM never
   retrains. This is why migration never requires retraining: only config
   moves, and config is just text files.

2. **The semantic layer does NOT port.** Snowflake Semantic Views and
   Databricks Metric Views are different, vendor-native formats. Accept
   rebuilding the semantic model per platform — this repo does not attempt to
   abstract that. (If true cross-platform semantic neutrality is ever
   required, the answer is a neutral layer like dbt Semantic Layer/MetricFlow
   or the emerging Open Semantic Interchange standard — not this loop.)

3. **Lock-in is prevented by OWNERSHIP, not just by the loop.** The loop
   preserves orchestration. It does nothing by itself for tuning that
   accumulates inside a vendor. That's why `exemplars/` and `evals/` exist:
   they're the git-owned home for verified Q→SQL pairs and golden questions
   that would otherwise pool inside Cortex/Genie. Rule of thumb: "own it or
   lose it" — only worth externalizing assets that are high-value and
   hard-to-rebuild (a handful of curated verified queries), not routine
   vendor telemetry.

4. **Verified queries ≠ every generated SQL.** `exemplars/*.yaml` holds a
   small, hand-curated set of Q→SQL pairs (tens, not thousands) — the ones
   worth blessing. Never log every runtime-generated query here; that's
   disposable and regenerates identically on any platform.

5. **Don't over-share into `shared/`.** Something belongs in `shared/` only
   when ≥2 packs genuinely want it identical (tone, SQL safety, base rubric
   shape). A pack's persona and domain skills stay in the pack even if that
   means some duplication — a bloated shared file with per-domain
   conditionals is worse than duplication.

6. **Cortex Agents / Genie are an alternative to this loop, not a base for
   it.** Using them means handing them orchestration instead of `engine/`.
   The clean bridge: expose Cortex Analyst / Genie AS a tool behind
   `SQLTool`, called BY this loop — don't try to build this loop "on top of"
   them.

7. **Build the loop only when it earns its keep**: cross-domain questions
   (needs a supervisor to call multiple domain agents-as-tools), your own
   eval/guardrails, or multi-step planning. For a single-domain BI Q&A agent,
   Cortex Analyst / Genie alone is often simpler and sufficient — don't
   over-engineer.

8. **Two hosting shells exist and are architecturally parallel** — pick
   whichever platform you're deploying to; `engine/`, `shared/`, `usecases/`
   are identical either way:

   | | Databricks | Snowflake |
   |---|---|---|
   | shell | `platform_databricks/agent.py` (MLflow `ResponsesAgent`) | `platform_snowflake/agent.py` (FastAPI) |
   | packaging | Models-from-Code | Docker image |
   | registry | Unity Catalog | Snowflake image repository |
   | compute | Model Serving endpoint | SPCS compute pool |
   | chat UI | Databricks App | Streamlit-in-Snowflake, or a custom MCP server so Cortex Agents can call it as a tool |

   Both were verified working end-to-end on the MOCK provider (FastAPI shell:
   `GET /healthz` → 200, `POST /invoke` → 18/18 real DQ answer). Adding a
   third platform = one more `platform_<name>/` folder; never touch the other
   two or `engine/graph.py`.

## Running it

```bash
pip install langgraph pyyaml
python run_local.py                        # dq_qals by default
python run_local.py kpi_analytics
python run_local.py anomaly_rca
python run_local.py parity_hana_snowflake
python run_evals.py <use_case>             # golden-set pass/fail
pytest -q                                  # unit tests (tests/): memory + fill/verdict
```

Flip to a real platform, no code change:
```bash
WORKER_PROVIDER=cortex      SQL_TOOL=cortex   python run_local.py <use_case>
WORKER_PROVIDER=databricks  SQL_TOOL=genie    python run_local.py <use_case>
WORKER_PROVIDER=anthropic   SQL_TOOL=mock     python run_local.py <use_case>
```

Worker and evaluator are selected **independently**, each by a provider + model, so
you can run a different model under the same provider or two different providers.
`auto` (or unset) = the sensible default:

|        | provider            | model        | `auto`/unset resolves to |
|--------|---------------------|--------------|--------------------------|
| worker | `WORKER_PROVIDER`      | `WORKER_MODEL`  | `mock` / provider's default model |
| judge  | `EVAL_PROVIDER` | `EVAL_MODEL` | same provider as worker / provider's default model |

```bash
# same provider, different models for worker vs. judge
WORKER_PROVIDER=cortex  WORKER_MODEL=llama3.1-70b  EVAL_MODEL=claude-3-5-sonnet  python run_local.py <use_case>
# different providers entirely
WORKER_PROVIDER=cortex  EVAL_PROVIDER=anthropic  EVAL_MODEL=claude-sonnet-4-5  python run_local.py <use_case>
```
Precedence per role: `WORKER_MODEL`/`EVAL_MODEL` > `<PROVIDER>_MODEL` > built-in default.
The judge runs only on the short rubric pass (`evaluate` in `engine/graph.py` uses
`get_eval_client`); a different judge model breaks the self-grading blind spot (a
model shouldn't score its own output). By design the evaluator scores and drives
answer-refinement only — it never rewrites the question or the SQL (accuracy stays
grounded in the native tool).

## Observability (a self-contained, optional module)

The platforms capture INFRA calls (endpoint I/O, tokens, the SQL a tool ran) but NOT
this loop's decision trail (iterations, per-step score, the judge's reason). We OWN that
emission — and ALL of it lives in ONE file, `engine/tracing.py`. The loop (`graph.py`)
stays pure: it imports `instrument` and wraps each node at registration
(`add_node("generate", instrument("generate", generate, use_case))`), and call sites use
`traced_invoke(graph, state, use_case)` instead of `graph.invoke(...)`. To change or
extend observability, edit only `tracing.py`.

```bash
TRACER=stdout      # DEFAULT, zero-dep: one JSON event per step to stdout
TRACER=otel        # OpenTelemetry spans (run=trace, node=child span) -> OTLP or console
TRACER=mlflow      # Databricks: MLflow Tracing spans (stubbed)
TRACER=eventtable  # Snowflake: SPCS event-table rows (stubbed)
TRACER=none        # disable
```
Mature tools are OPTIONAL adapters, off unless you switch to them AND install their (lazy)
deps — the default `stdout` path needs nothing extra. `otel` uses `opentelemetry-sdk`
(+ the OTLP exporter); it exports to `OTEL_EXPORTER_OTLP_ENDPOINT` if set, else prints
spans to the console. Events: `run_start · generate · evaluate · refine · run_end`, each stamped with a
per-invocation `run_id` (also returned by the Snowflake shell's `/invoke`) so loop events
JOIN to the platform's SQL/token rows. By default events carry **metrics only**; set
`TRACE_INCLUDE_CONTENT=true` to also include answer/verdict TEXT (opt-in, for privacy). `StdoutTracer` is the portable core — both
platforms collect stdout (SPCS → event table; Databricks serving → serving/inference
logs). Do NOT rebuild token/cost telemetry here — read it from Cortex usage views /
Databricks system tables and correlate by `run_id`.

**Guarantees (do not regress these):** (1) **zero accuracy/token/cost impact** — tracing
never touches prompts/answers/scores/flow and makes no model or SQL calls; (2) **zero
overhead when `TRACER=none`** — `instrument` returns the bare node function, no wrapper;
(3) **fail-safe** — the node runs first and any tracer error is swallowed, so a broken
sink can never break a run. Latency when enabled is a `json.dumps` + a stdout write per
event (sub-ms vs. LLM calls) — for real network-backed sinks, batch/flush async.

## Memory (episodic + feedback flywheel — a self-contained, optional module)

All memory code lives in ONE file, `engine/memory.py` (same seam pattern as the others).
It owns the *learning-flywheel feedstock*: every finished run (episodic) and any thumbs/
notes (feedback), as **append-only rows you own and can export** — the raw input you later
curate by hand into `usecases/*/exemplars` + `evals` (git). The loop stays pure; capture is
applied at the call-site boundary via `remember_run(final, use_case)` after `traced_invoke` (it
acquires the memoized store itself and is fail-safe), and the Snowflake shell adds a
`POST /feedback` endpoint. Adapters validate `run_id` and close SQLite connections; an
unknown `MEMORY_STORE` name is rejected (never silently disabled); `/feedback` reports the
truthful status (`recorded` / `memory_disabled` / `memory_error`). Tests: `tests/`.

```bash
MEMORY_STORE=none        # DEFAULT (opt-in). Nothing persisted.
MEMORY_STORE=mock        # in-memory (tests/inspection)
MEMORY_STORE=sqlite      # local file (MEMORY_SQLITE_PATH); durable local dev
MEMORY_STORE=snowflake   # PORTABLE_AGENT_INTERACTIONS/_FEEDBACK tables via Snowpark (prod)
```
Guarantees: episodic capture is **fail-safe** (`remember_run` swallows store errors — a bad
sink never breaks an answer) and makes **no model/SQL-generation calls** (zero token/cost).
Deliberately **scoped**: we build episodic+feedback only. **Skipped:** semantic/RAG vector
memory (least portable; wrap Cortex Search / DBX Vector Search instead). **Parked:**
conversation threads/multi-turn (needs compaction), runtime recall / auto-consolidation
(would pollute curated exemplars), and a Databricks/Delta adapter. Don't add these to the
loop; if built, they go behind `MemoryStore` in `engine/memory.py`. Once you persist
question/answer text you own its PII/retention — rows are append-only + `run_id`-keyed so
deletion-by-request is a plain `DELETE`.

## Adapter status (which are wired vs. stubbed)

- **Cortex is WIRED** (Snowflake): `CortexClient` (COMPLETE) and `CortexAnalystTool`
  (Analyst REST → runs the generated SQL) in `engine/llm_client.py` / `engine/sql_tool.py`.
  Auth is shared via `snowpark_session()` + `snowflake_bearer_headers()`: inside SPCS they
  use the injected OAuth token at `/snowflake/session/token`; locally a SINGLE `SNOWFLAKE_PAT`
  authenticates BOTH the Snowpark session (as the password) and the Analyst REST call (bearer)
  — no `SNOWFLAKE_PASSWORD`. Needs a warehouse and a
  semantic layer — EITHER `CORTEX_SEMANTIC_VIEW` (an existing native Semantic View) OR
  `CORTEX_SEMANTIC_MODEL` (a stage YAML); view wins if both set. `CortexClient` uses the STRUCTURED
  COMPLETE form (messages+options) to get `usage`, so it detects truncation via
  `completion_tokens >= max_tokens` (Cortex exposes no `finish_reason`), falling back to the plain
  string form if that call fails — never a regression. Verified: imports stay lazy (mock path untouched),
  row formatter + auth-error paths unit-tested; the live call is untested here (no account).
- **Databricks/Genie are STILL STUBBED**: `DatabricksClient` / `GenieTool` have the SDK
  shape + a `TODO`. Wire like Cortex, and test with `SQL_TOOL=mock` first to isolate the
  LLM path from the SQL path.

## Deploying on Databricks

Recipe is the docstring at the top of `engine/platform_databricks/agent.py`:
MLflow Models-from-Code (`code_paths=["engine","shared","usecases"]`) → Unity
Catalog registration → `agents.deploy()` → Databricks App. That file is the
ONLY thing you rewrite if you ever leave Databricks. Beginner step-by-step:
`docs/deployment/deploy-databricks.md` (separate from the code; MOCK-first).

## Deploying on Snowflake (SPCS)

Recipe is the docstring at the top of `engine/platform_snowflake/agent.py`:

```bash
docker build -t <repo_url>/dq_agent:latest -f engine/platform_snowflake/Dockerfile .
docker push <repo_url>/dq_agent:latest
snow spcs compute-pool create dq_pool --family CPU_X64_XS --min-nodes 1 --max-nodes 1 --auto-resume
snow spcs service create dq_agent --compute-pool dq_pool --spec-path engine/platform_snowflake/spec.yaml
# then: SHOW ENDPOINTS IN SERVICE dq_agent;  -> the live URL
```

Beginner step-by-step (separate from the code): `docs/deployment/deploy-snowflake.md`.
The container was validated for real (image builds, `/healthz` 200, `/invoke` 18/18,
JSON trace events on stdout). Notes: the image installs
`engine/platform_snowflake/requirements.txt` (minimal Snowflake set — NOT the repo-root
requirements), `spec.yaml` uses the SPCS `env:` **map** form (not the k8s list) and ships
**MOCK-first** so the first deploy works before the Cortex adapters are wired. Fill the
`image:` path from `SHOW IMAGE REPOSITORIES`. Cortex adapters are **wired**: they use the
OAuth token Snowflake injects at `/snowflake/session/token` (via `snowpark_session()` /
`snowflake_bearer_headers()`), so `WORKER_PROVIDER=cortex`/`SQL_TOOL=cortex` works from
inside the service once you set `CORTEX_SEMANTIC_MODEL` + a warehouse (see the cortex block
in `spec.yaml`). Spec still ships MOCK-first so the first deploy needs none of that.

Local test before deploying (no Docker/Snowflake needed):
```python
from fastapi.testclient import TestClient
from engine.platform_snowflake.agent import app
client = TestClient(app)
client.get("/healthz")                                   # {"status": "ok", ...}
client.post("/invoke", json={"question": "..."})          # {"answer": ..., "score": ...}
```

## Ownership tiers (what moves on migration)

```
█ engine/ + shared/ + usecases/   yours, portable   → unchanged on any platform
▒ platform_databricks/            ~25-line glue     → the only file you rewrite
░ Cortex / Genie / Databricks     rented infra      → swapped for the new vendor's
⚪ semantic model + raw data       native            → rebuilt per platform (accepted)
```

## Repo sync status (check before assuming the remote matches local)

The GitHub remote (`github.com/NagarajDE/portable-agent`) has, at last check,
lagged behind the local/canonical build by several rounds of changes —
confirm before relying on it. Known-missing-as-of-last-check items included:
`CLAUDE.md` itself, `.gitignore`, the real `dq_qals` rubric + its
`dq_dimensions.md`/`severity_and_triage.md` skills, the entire
`kpi_analytics/` and `anomaly_rca/` packs, and all of `platform_snowflake/`.
If you're picking this repo up fresh, run the local test commands above to
confirm which pieces are actually present before assuming this file is
in sync with `git status`.

## Conventions for this codebase specifically

- Prefer plain files (`.md`, `.yaml`) over code for anything domain-specific —
  that's what keeps a pack editable by a non-engineer and diffable in git.
- Prompt filling uses `fill()` — a **single-pass** regex replace of `{key}` (not
  `str.format()` and not sequential `str.replace`). Single-pass so a substituted value
  containing `{other}` is never re-interpreted; brace-safe so literal braces in SQL/JSON
  examples and unknown `{names}` are left untouched.
- Keep `agent.py` (the Databricks shell) free of business logic — it should
  only ever import and expose, never contain a prompt string or a rule.
- The judge is **grounded**: `evaluate()` passes the retrieved `data` into the rubric so the
  judge can reject fabricated numbers (the rubric marks the candidate answer untrusted and
  delimits it). The reply is parsed into a validated `Verdict(score, reason)` via
  `parse_verdict()` (plain Pydantic, not a regex scrape) — accepts the line form
  `SCORE: N/<max> - <reason>` or a JSON object, **rounds** (not truncates) scores, **rejects a
  mismatched denominator** and non-finite scores, re-asks `eval_retries` times, then falls back
  to score 0. Don't "simplify" back to a bare regex, and do NOT pull in an agent framework
  (Pydantic AI / instructor) for the generic core.
- **Refine builds from the BEST answer, not the latest** (`refine` uses `best_answer` +
  `best_feedback`, not `answer`/`feedback`). Otherwise a degraded revision becomes the base and
  the loop walks downhill while paying full LLM calls. Don't "simplify" refine back to `s["answer"]`.
- **Scoring knobs are separate:** `max_score` (validation cap, default 18) vs `pass_score`
  (stop threshold, must be ≥1; `threshold` is the backward-compatible alias, **default 15** in
  `shared/config.yaml`). Keep it BELOW `max_score`: `pass_score == max_score` means only a perfect
  score stops early, so a strict real judge runs every question to `max_iters` (worst-case latency
  as the normal case). `keep_going` stops at `pass_score`; `parse_verdict` validates against `max_score`.
  Report scores out of `max_score`, never `pass_score` (see `run_local.py`). `max_score` is
  genuinely configurable end-to-end: rubric prompts template the denominator as `SCORE:
  N/{max_score}` (filled in `evaluate`), the body's point-total says `{max_score}-point`, and
  the MockClient reads that scale from the prompt. Caveat: `max_score` is the rubric's *authored
  total*, not an auto-rescaler — the axis weights (e.g. three `/6`) are the pack author's job to
  make sum to `max_score`. Shipped rubrics are 18 (three `/6`). Keep the `{max_score}` placeholder;
  don't hardcode a number.
- **Model-generated SQL is treated as untrusted:** `CortexAnalystTool` runs it only through
  `_ensure_read_only()` (single statement, SELECT/WITH only) with a `SQL_TIMEOUT_SECONDS` cap —
  a backstop; the PRIMARY control is granting the service role SELECT-only (see the deploy guide).
- Keep observability OUT of the loop. `graph.py` nodes are pure; all tracing lives in
  `engine/tracing.py` and is applied via `instrument(...)` (node wrapper) + `traced_invoke`
  (run wrapper). Don't add `tracer.event(...)`, timing, or vendor observability SDKs
  (mlflow/snowflake) into `graph.py`. `log()` stays for human console output (verbose);
  structured queryable events are the Tracer's job. Preserve the three guarantees above.
- When editing a rubric or skill, prefer specificity to your actual domain
  (real thresholds, real table/column names) over generic placeholders — the
  shipped rubrics here are realistic starting points, not tuned to any one
  company's definitions.
