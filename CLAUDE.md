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
  graph.py             COMPOSITION ROOT: validate config → build deps → wire the graph (State, build_graph);
                       `loop: true` wires evaluate/refine, else generate → END (worker-only, unscored)
  nodes.py             the loop's nodes (generate·evaluate·refine + routers) as functions of a Runtime
  retrieval.py         ONE typed Retrieval + Retriever strategies (sql|deterministic|agentic|planned|toolless)
  config.py            PackConfig — typed, closed (extra=forbid), bounded pack config
  packs.py             reading a pack: config merge, prompts, skills, instructions, exemplars, fill()
  verdict.py           Verdict + parse_verdict (JSON-authoritative, fail-closed)
  guards.py            rewrite_drift(): the deterministic reformulation guards
  settings.py          the loop's deployment knobs from process env (read once, passed down)
  output.py            opt-in declared output_schema validation (the structured-output seam)
  llm_client.py         LLMClient interface + mock|anthropic|cortex|databricks
  sql_tool.py           SQLTool  interface + mock|cortex-analyst|genie
  tools/                generic Tool layer: base(contracts)+registry+dispatch+orchestrator
                        + adapters sql_bridge|mock|http  (SQL = one tool category)
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
  semantic_layer.yaml  ONLY AI+BI/Analyst packs — the native view/model this pack queries
  prompts/generate.md  the persona — the one file that's truly per-agent
  prompts/rubric.md    ONLY if this domain needs different scoring than shared
  skills/*.md          ONLY domain-specific how-to (not reporting/safety — inherited)
  exemplars/*.yaml      verified Q→SQL pairs (your portable few-shot / seed)
  evals/golden_set.yaml your portable eval questions
  fixtures.py           MOCK-only demo data; ignored once SQL_TOOL/WORKER_PROVIDER are real
```

### Composition rules (implemented in `engine/graph.py`; full guide: `docs/concepts/use-case-pack-anatomy.md`)
- **skills**: `shared/skills/*.md` + `pack/skills/*.md` → CONCATENATED. A pack may drop specific
  shared skills via `exclude_shared_skills: [<stem>]` in its config (e.g. a non-SQL pack excludes
  `sql_safety` so SELECT-only rules aren't injected).
- **prompts**: `pack/prompts/<f>` if it exists, ELSE `shared/prompts/<f>` → OVERRIDE (by FILE
  PRESENCE — never declared in config)
- **config**: `shared/config.yaml` merged with `pack/config.yaml` (pack wins) via
  `inherits: [shared]`
- **exemplars**: `pack/exemplars/*.yaml` few-shot; an optional `kind: text_to_sql | input_output`
  picks the render format (else inferred from the presence of an `sql` key)
- **semantic_layer**: `pack/semantic_layer.yaml` (AI+BI/Analyst packs ONLY) declares the native
  semantic view/model; **presence marks the pack AI+BI-backed** (absent → engine never looks). Read
  from the PACK, never env — `CORTEX_SEMANTIC_VIEW`/`_MODEL` are a test-only override in the runners.

### Adding a new agent = adding a folder, never touching `engine/` or `shared/`
```
usecases/my_new_agent/
  config.yaml          inherits: [shared], name, sample_task
  prompts/generate.md  persona (+ {skills}{exemplars}{data} placeholders)
  exemplars/  evals/    your tuning
  # semantic_layer.yaml  ONLY if AI+BI/Analyst-backed (native view/model)
  # rubric.md / refine.md only if overriding shared
```
Fastest start: copy `usecases/_TEMPLATE/` (a runnable mock pack). Step-by-step guide + the new-pack
checklist: `docs/concepts/use-case-pack-anatomy.md`.

## Existing use-case packs (reference examples)

| pack | mode | rubric axes |
|---|---|---|
| `dq_qals` | is the data right? | rule correctness · evidence & quantification · actionability |
| `kpi_analytics` | what does the metric say? | metric-definition correctness · time grain & framing · clarity |
| `anomaly_rca` | why did it change? | hypothesis quality · evidence & isolation · actionable conclusion |
| `parity_hana_snowflake` | cross-engine parity | overrides the shared rubric (demonstrates override, not inherit) |
| `api_assistant` | generic **non-SQL** example | demonstrates the tool layer: a mock catalog + the generic http tool (mock mode); no creds, no network |
| `incident_triage` | **agentic** tools example | `tool_mode: agentic` — the MODEL chooses which read-only tools to call (catalog · http health · deploys), then triages; mock/no-creds (point at a real model to see tools fire) |
| `ops_rca` | **planned** multi-step, NON-SQL | `tool_mode: planned` — a validated DAG of read-only mock tools (detect → health → deploys), chained; creds-free (needs a real/scripted planner; bare mock escalates) |
| `inventory_excess_disposition` | **planned** multi-step, SQL/BI | `tool_mode: planned` over `INVENTORY_ANALYTICS` — rank excess → forward demand for THOSE materials (`{{s1}}`) → classify Hold/Reduce/Redistribute/Disposition; the chain a single query can't produce |

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
you can run a different model under the same provider or two different providers. (The evaluator
only runs for packs with `loop: true`; a third optional role, the planner — `PLANNER_PROVIDER` /
`PLANNER_MODEL` or pack `models.planner` — serves `tool_mode: planned` packs, else the worker plans.)
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
  semantic layer, which the PACK declares in `semantic_layer.yaml` (`snowflake: {view | model_file}`;
  view wins if both). `engine/` reads it from the pack, NEVER env — `CORTEX_SEMANTIC_VIEW` /
  `CORTEX_SEMANTIC_MODEL` are only a TEST-time override applied by the runner scripts. `CortexClient` uses the STRUCTURED
  COMPLETE form (messages+options) to get `usage`, so it detects truncation via
  `completion_tokens >= max_tokens` (Cortex exposes no `finish_reason`), falling back to the plain
  string form if that call fails — never a regression. `snowpark_session()` is **memoized** — ONE
  session serves worker + judge + Analyst tool (+ Snowflake memory), not 3-4 logins; it runs
  statements serially, so it's single-flight per process (fine at low concurrency; pool per-thread
  for high concurrency). Verified: imports stay lazy (mock path untouched),
  row formatter + auth-error paths unit-tested; the live call is untested here (no account).
- **LiteLLM is the ADOPTED AI GATEWAY** (like LangGraph is the adopted loop): `LiteLLMClient`
  (provider `litellm`) is ONE adapter reaching every provider via the model string
  (`anthropic/…`, `databricks/<endpoint>`, `openai/…`, `azure/…`, `bedrock/…`). We do NOT build
  routing/fallback — that's LiteLLM's, in-process (SDK, default) or a proxy via `LITELLM_BASE_URL`
  (LiteLLM Proxy / Databricks AI Gateway). `litellm` is lazy-imported (mock path unaffected). Prefer
  it for all non-Cortex models; `DatabricksClient` is superseded (back-compat only). See
  `docs/concepts/ai-gateway.md`.
- **Genie is WIRED** (Databricks): `GenieTool` (`SQL_TOOL=genie`) mirrors `CortexAnalystTool` — one
  stateless question per call via the Genie Conversation API (`databricks-sdk`, lazy; memoized
  `databricks_workspace_client()` = unified auth, the analog of `snowpark_session()`), the generated SQL
  is captured as `last_sql` and passed through `_ensure_read_only()`, Genie's own query result is the
  evidence (`SQL_MAX_ROWS` cap + visible 'omitted' note), zero rows / a clarification → the `NO_DATA`
  sentinel. The pack declares `databricks: {genie_space: <id>, metric_view: <c.s.mv>}` in
  `semantic_layer.yaml` — `genie_space` is REQUIRED (Genie is addressed by space; a metric view alone is
  not callable), `metric_view` is optional and, with `DATABRICKS_WAREHOUSE_ID`, enables the same
  `dimension_paths()` / `distinct_values()` binding capabilities as Cortex (DESCRIBE TABLE + GROUP BY).
  Unit-tested against a fake SDK client (the `client=` seam); **the live call is untested here.**

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

- **The loop is OPT-IN per pack: `loop: true` (default OFF = NON-LOOP).** Missing / null / blank /
  false → `generate → END`: framing, retrieval (single SQL, deterministic sweep, agentic, **planned
  multi-step**), reformulate-and-retry and the no-data escalation ALL still run; only the judge and
  refine are removed and the answer is UNSCORED (`best_score` stays -1 → surfaces report `score=None`
  with `status=ok`; the `run_end` event carries `scored`). `loop: true` with no buildable evaluator →
  `RuntimeWarning` + non-loop, never a failed build (an evaluator inheriting the worker is configured,
  not missing). `shared/config.yaml` deliberately does NOT set it; every shipped pack and `_TEMPLATE`
  set `loop: true` (a test enforces this). Don't put `loop: true` in shared; don't make the default
  loop; don't route a non-loop run through `evaluate`. The unscored-output contract (`status` = usable
  vs escalated, `score` = judged or not, two independent axes) is documented in
  `docs/concepts/the-refine-loop.md` §0 — keep both surfaces on it.
- **Planner budget + planner model (planned mode).** The plan call passes its OWN output cap
  (`PLAN_MAX_TOKENS`, default 8192, never below `LLM_MAX_TOKENS`) via `complete(prompt, max_tokens=…)` —
  every adapter honors a per-call override; a truncated plan escalates with an ACTIONABLE reason naming
  `PLAN_MAX_TOKENS`. An optional third role, `models.planner` / `PLANNER_PROVIDER`+`PLANNER_MODEL`
  (`get_planner_client`, same env > pack > default precedence and M6 rule), emits the DAG; unset = the
  worker plans. Don't raise the general cap to fix plan truncation, and don't add a fourth role without
  a seam like this one.
- **Remote MCP (`url:`) is allowlisted, https-only, env-token-only.** `type: mcp` takes exactly one of
  `command` (stdio) or `url` (Streamable HTTP; `transport: sse` for legacy); a remote host must be in the
  tool's `allow_hosts` (else `MCP_ALLOWED_HOSTS`; empty = deny), must not resolve to a private address
  (re-checked before each connect), and the bearer token is the env var NAMED by `token_env`. Arguments
  are validated against the server's `list_tools` `inputSchema` (cached per tool) before `call_tool`,
  with value-free messages; non-text blocks surface as labeled markers. Misconfiguration fails at
  build time. All of it still rides `dispatch()`.
- **The core's shape (post-redesign; see `docs/design/target-design.md`).** `graph.py` is ONLY a
  composition root; nodes live in `nodes.py` as functions of an explicit `Runtime` (add a dependency to
  `Runtime`, never a closure). Retrieval is ONE abstraction: a `Retriever` strategy returning a typed
  `Retrieval(text, empty, hint)` — the loop asks `retriever.frames_question` / `.reformulates` and never
  branches on `tool_mode`; adding a retrieval kind = one class in `retrieval.py`, zero loop edits. The
  `NO_DATA` sentinel is a text-adapter WIRE FORMAT interpreted in exactly one place
  (`Retrieval.from_text`) — no other module may sniff it. **SQL always runs through `dispatch()`**
  (the single-SQL path is wrapped, not bypassed). Pack config is `PackConfig` (`extra="forbid"`): an
  unknown key is a build-time error, never a silent no-op — add new knobs THERE with their bounds.
  Two opt-in loop-contract seams exist, default off: `output_schema:` (enforced shape → violation scores
  0 and refine fixes the shape) and `judge_verify_tool:` (the judge spot-checks with one declared
  read-only tool). `thread_id` rides on State for a future multi-turn capability.
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
- **A failed refinement is NON-FATAL; a failed first generate IS fatal.** `refine` catches
  `RuntimeError`/`EmptyResponseError` from the worker (e.g. truncation) and ends the loop keeping
  the already-scored `best_answer` — a mid-loop worker error must not 500 away good work. `generate`
  stays unguarded (nothing to fall back to on the first draft). Don't guard `generate`.
- **No-progress stop:** `max_stall` (default 2) ends the loop after that many refines that don't
  beat `best_score` — guards the deterministic refine-from-best case (same base+critique → same
  answer → grind to `max_iters`). `0` disables it.
- **Groundedness is DETERMINISTIC, never the judge's job.** A blank retrieval (no rows / no SQL) is
  marked with the `NO_DATA` sentinel at the tool boundary (`engine/sql_tool.py`: `no_data`/`is_no_data`/
  `data_hint`), NOT flattened into prose — because an honest "no data" answer is fully consistent with
  no-data evidence, so the rubric would score it as a *pass* (the exact bug). A blank isn't proof the
  data is missing (could be a misread question), so the SQL path reformulates the question and re-queries
  up to `max_data_retries` (default 1, ≤5; `shared/prompts/reformulate.md`). If it stays blank, `generate`
  sets `grounded=False` plus an escalation REASON in `status` and `after_generate` routes STRAIGHT to END — the judge NEVER
  runs, `best_score` stays -1, and the surfaces report score N/A + status so it reaches a human as "needs
  action", never a score. Don't route a no-data run through `evaluate`, and don't "fix" this in the rubric.
- **A reformulation must ask the USER's question.** "Rows came back" is NOT "the question was answered":
  asked to rewrite an unanswerable question, a model will happily ask one the semantic model *can*
  answer, get rows, and flip `grounded` to True — which earned a PASSING 16/18 for an honest "employee
  data isn't here" answer in live testing. So the rewrite is gated twice: the prompt may REFUSE with
  `NO_REFORMULATION` (subject not in this data), and the engine checks DETERMINISTICALLY that a
  distinctive word survived (`_keeps_subject`) and that no pinned identifier — `ZZ999`, `PO12345` — was
  dropped (`_keeps_pinned`). A refusal or a drift keeps the blank and escalates — it does NOT burn the
  remaining retry budget. **The two checks are separate because they mean different things to a human:**
  a SUBSTITUTED subject → `status="out_of_scope"` (this data can't answer that question; ask a different
  agent), a DROPPED identifier or a still-blank re-query → `status="no_data"` (the query ran and matched
  nothing; check the code/filters/freshness). Both are unscored (`score=None`). Retrying covers the paths
  that fire EXACTLY ONCE (single SQL call, deterministic tool sweep); `tool_mode: agentic` is excluded
  because it already self-corrects across `max_tool_steps`. The subject check folds plurals and uses a
  ≥3-letter floor, so short nouns (`tax`, `fee`) are visible and `salary`↔`salaries` isn't false drift.
  Known gaps, all in the SAFE (escalate, never false-pass) direction: a measure swap that keeps its
  dimension (`tax by region`→`fees by region`) still reads as kept — the `NO_REFORMULATION` refusal is
  the net; a re-expressed period code (`FY26`→`fiscal 2026`) reads as a dropped identifier and escalates
  as `no_data` rather than recovering (exempting period codes would let the period be dropped entirely,
  reintroducing the widening false-pass); and the agentic path does not yet mark a blank gather (a MOCK
  worker can't drive the ReAct protocol, so marking it would break every creds-free agentic run).
- **Term→value binding is HYBRID and mostly automatic (`engine/binding.py`).** Never hand-author every
  column's values. Layers, cheapest first: a pack's deliberate few (`dimension_map.md`, `glossary.yaml`,
  `recover_value_dims`) → an opt-in LOW-cardinality **value catalog** (`value_catalog: {max_cardinality:
  N}`; profiled once via `dimension_paths()`, injected into FRAMING as authoritative, high-cardinality dims
  skipped) → **auto recovery from the failed predicate** (always on: `CortexAnalystTool.last_sql` →
  `filter_columns()` → `resolve_dims()` → `distinct_values()` → the reformulate step). The Cortex
  capabilities (`last_sql`, `dimension_paths`, `distinct_values`) are OPTIONAL duck-typed methods on the
  SQL tool; mock degrades to skills-only. Don't add per-column value lists to skills — fix the mechanism.
- **Business glossary (`glossary.yaml`, shared + pack, merged by term, PACK WINS)** rides on the skills
  text into framing/planning AND generate/refine (`packs.load_glossary` / `render_glossary`). Shared holds
  only terms every pack defines identically (same rule as shared skills).
- **Per-pack `models:` are DEFAULTS; env still wins.** `databricks` has NO default model (explicit
  serving-endpoint name required; `DATABRICKS_HOST` may carry a path, it's stripped, never rejected).
- **`zero_is_no_data` is OPT-IN (default false).** A single all-zero/NULL row (`COUNT(*)=0`, `SUM=NULL`)
  is structurally a row, so the sentinel can't see it. Escalating it is only correct where 0 always means
  "a filter or status label matched nothing" — for a pack where 0 is a real answer, escalating is wrong.
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
- **Generic tool layer (`engine/tools/`): SQL is ONE tool category, not the engine's assumption.**
  Packs declare tools in `config.yaml` (`tools:`). Routing (in `load_tools`): ONLY an ABSENT
  `tools:` key → legacy `get_sql_tool().ask()` path (existing packs unchanged); `tools: []` → a
  deliberately TOOLLESS agent (no tools AND no SQL); `tools:` that is null or not a list → config
  error (null is treated as a mistake, never implicit legacy). Tool execution is **deterministic** (the engine runs read-only tools up front and feeds
  observations into `generate` ONCE per run; the LLM does NOT select tools — so tool output can't
  trigger a tool call, and refine never re-runs them). ReAct is deferred behind the same
  `dispatch()`. Invariants: every call goes through `dispatch()` (defensive `spec` access → approval
  gate → validate → timeout+`future.cancel()` → result-invariant enforcement → redacted+bounded
  output AND error → one redacted log line) and it NEVER raises; `read_only` is a **required**
  `ToolSpec` field (fail-closed authoring) and is **authoritative at dispatch** (an adapter may
  hardcode it, e.g. sql=read-only, or expose it as config for a tool that's legitimately either,
  e.g. a mutating GET); side-effecting tools run only when `ctx.approved is True` (strict); keep any
  vendor SDK import lazy (inside `run`). The `http` tool binds its bearer token to the ORIGINAL https
  origin (never forwarded across a redirect or over an http downgrade), uses a `trust_env=False`
  session, re-validates host+SSRF before every connect, and applies a wall-clock deadline over the
  retry/redirect loop (re-checked between body chunks). Residuals (accepted): the resolved IP is not
  pinned to the socket (the **allowlist is the primary control** against DNS rebinding), and DNS, the
  rate-limit sleep, and a single blocking read sit outside the deadline — the dispatch
  `future.result(timeout)` is the hard caller-side bound. See
  [`docs/concepts/tools-and-agents.md`](docs/concepts/tools-and-agents.md).
- Keep observability OUT of the loop. `graph.py` nodes are pure; all tracing lives in
  `engine/tracing.py` and is applied via `instrument(...)` (node wrapper) + `traced_invoke`
  (run wrapper). Don't add `tracer.event(...)`, timing, or vendor observability SDKs
  (mlflow/snowflake) into `graph.py`. `log()` stays for human console output (verbose);
  structured queryable events are the Tracer's job. Preserve the three guarantees above.
- When editing a rubric or skill, prefer specificity to your actual domain
  (real thresholds, real table/column names) over generic placeholders — the
  shipped rubrics here are realistic starting points, not tuned to any one
  company's definitions.
- **Operator INSTRUCTIONS are a first-class layer** (`load_instructions`, composed like skills from
  `shared/instructions/*.md` + `pack/instructions/*.md`; `exclude_shared_instructions:` opt-out).
  Injected into generate/refine/rubric via `_fill()`: a prompt with `{instructions}` controls
  placement, else the block is AUTO-PREPENDED only when non-empty — so packs with no instruction
  files are byte-for-byte unchanged. Tier-1/operator-trust: never promote user text here. Per-request
  `instructions` are OFF unless `allow_runtime_instructions: true`. See `docs/concepts/instructions.md`.
- **Sample questions / manifest:** `pack_manifest(use_case)` returns `{name, description,
  sample_questions}`; `sample_questions:` in config, else defaults to the golden-set questions.
  Exposed at Snowflake `GET /manifest` and the Databricks `"__manifest__"` request. Non-secret.
- **Model profiles:** a pack may set `models: {worker/evaluator: {provider, model}}` as DEFAULTS;
  **env still wins** (precedence env > pack `models:` > built-in default), preserving the one-env-var
  migration flip. `get_llm_client`/`get_eval_client` take `cfg_models`.
- **Agentic tools (opt-in) + MCP:** `tool_mode: agentic` (default `deterministic`) enables a bounded,
  model-driven READ-ONLY gathering loop (`engine/tools/agentic.py`, prompt `shared/prompts/act.md`,
  `max_tool_steps` <= 10) — prompt-based JSON tool-calls (NO `LLMClient` change), every call still via
  `dispatch()`, writes never auto-run. It weakens the deterministic path's injection-safety (documented
  trade-off). `type: mcp` (`engine/tools/mcp_tool.py`) exposes an MCP-server tool as a `Tool`,
  `read_only` fail-closed; live call lazy + untested. See `docs/concepts/tools-and-agents.md` §9.
- **Planned multi-step (opt-in):** `tool_mode: planned` (`engine/tools/planned.py`, prompts
  `shared/prompts/plan.md` + `replan.md`) is for a KNOWN, repeatable multi-step shape where step N's
  result shapes step N+1. The model commits a JSON **DAG**; `parse_plan` validates it DETERMINISTICALLY
  **before anything runs** (allowlist of the pack's tool labels · unique ids `^s[1-9][0-9]*$` · deps ⊆
  ids · every `{{sN}}` declared as a dep · acyclic · ≤ `max_plan_steps`); the engine executes it,
  substituting a BOUNDED (≤600-char, single-line, sanitized, single-pass) summary for each `{{sN}}`, and
  REPLANS on a step failure (≤ `max_replans`; **successful results are frozen/immutable**). Invariants
  (don't regress): every step goes through `dispatch()` with `approved=False` (**writes refused**); a
  step whose output `is_no_data()` or is blank is `empty`, **NOT evidence**; zero evidence → the
  `NO_DATA` sentinel → the existing escalation (judge never scores an empty answer); `planned` is
  EXCLUDED from `_frame_question` and the reformulate loop (the planner owns query formulation, replan
  is its self-correction). The planner sees `plan_skills` (else `frame_skills`, else full skills). It is
  stronger than agentic (deterministic validation + declared refs + allowlist) but weaker than pure
  deterministic (a result parameterizes a later, still-read-only, query). Per-step tracing
  (`plan_created`/`plan_step`/`replan`) via `trace_event` in `engine/tracing.py`. Full guide +
  MODE-SELECTION test: `docs/concepts/multi-step-planning.md`.
