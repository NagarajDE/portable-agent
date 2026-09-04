# Portable Agent — Project Context & Goals

This document captures **why this project exists, what we're trying to achieve,
and every design decision we made** — the full narrative. `CLAUDE.md` is the
operational spec (how to work in the code); this file is the reasoning behind it.

---

## 1. The goal (north star)

> Build a **portable, vendor-agnostic agentic platform** so that agents — and the
> tuning accumulated into them over time — are **not locked into Snowflake or
> Databricks**. Migrating between platforms should mean *swap an adapter + rebuild
> the semantic layer*, never *retrain from scratch*.

Success = the day we move platforms, we keep: the orchestration loop, prompts,
skills, rubric, verified Q→SQL pairs, and golden eval sets — unchanged.

---

## 2. Why we started (the original question)

The session opened with a worry:

> "When we migrate agents to a new platform, do we lose all the trained/accumulated
> info? Do we have to retrain them? e.g. agents built on Snowflake or Claude Code,
> then moving to Databricks."

Everything below is the answer to that question, worked out in stages.

---

## 3. Core realizations (concepts we clarified)

| Assumption going in | What's actually true |
|---|---|
| Agents are "trained" and migration loses that training | Agents improve via **config** (prompts, verified queries, rubric), not model weights. The base LLM never retrains. |
| Migrating = retrain the model | Nothing to retrain. Swap the inference call; the model is an API either way. |
| Embeddings are "trained in" | An embedding is text → a vector on one model's "map". Migrating to a different embedding model = **re-embed the corpus** (a batch job), not retrain. |
| The semantic view will carry over | It won't. Cortex Semantic Views ≠ Databricks Metric Views — different native formats. **Rebuild per platform** (accepted, like migrating data). |
| A portable loop prevents lock-in | Only half of it. Lock-in is prevented by **ownership** — where your tuning physically lives — not by the loop alone. |
| There's a "fine-tune the agent" button | For BI agents, improvement = **curation** (semantic model + verified queries + instructions), which is portable if you own it, locked-in if the vendor hosts it. |

---

## 4. What we're building (the solution shape)

A LangGraph loop (generate → evaluate → refine, scored on an /18 rubric) sitting
behind stable interfaces, so the model and the database are swappable components.

```
        ┌─────────────────────────────────────────────┐
        │  YOUR AGENT  (portable)                       │
        │  loop · prompts · skills · rubric · exemplars │
        │  talks ONLY to interfaces, never a vendor SDK │
        └───────────────┬───────────────┬──────────────┘
                        │               │
                  LLMClient          SQLTool
                        │               │
                        ▼               ▼
            Cortex / Databricks     Cortex Analyst / Genie
            / Anthropic / Ollama    (text-to-SQL as a tool)
```

Switch provider = one env var. Switch database tool = one env var. The loop never
changes.

---

## 5. The three-tier architecture

| Tier | Contains | Shared? | Lives in |
|---|---|---|---|
| **Engine** | loop, adapters, eval runner, hosting shells | 100% shared | `engine/` |
| **Shared** | base rubric, refine prompt, reporting + SQL-safety skills, config defaults | inherited by packs | `shared/` |
| **Use-case pack** | persona, domain skills, verified Q→SQL, golden set, config | one per agent | `usecases/<name>/` |

Composition rules (in `engine/graph.py`):

| Asset | How it composes |
|---|---|
| skills | `shared/skills/*` **+** `pack/skills/*` (concatenate) |
| prompts | `pack/prompts/<f>` if present, **else** `shared/prompts/<f>` (override) |
| config | shared defaults **+** pack config, pack wins (`inherits: [shared]`) |

**Adding a new agent = adding a folder.** Never touch `engine/` or `shared/`.
Real cost of a new agent: one persona paragraph + its own tuning.

---

## 6. Decision log (the "why", so we don't re-argue these)

1. **Improvement is config, not weights** → migration never needs retraining.
2. **Semantic layer does not port** → rebuild per platform is accepted; this repo
   does not try to abstract it. (True neutrality, if ever required, = dbt Semantic
   Layer / MetricFlow or the emerging Open Semantic Interchange standard — not this loop.)
3. **Lock-in is prevented by ownership** → `exemplars/` and `evals/` exist so
   verified Q→SQL and golden questions live in git, not pooled inside Cortex/Genie.
   Rule: "own it or lose it" — externalize only high-value, hard-to-rebuild assets.
4. **Verified queries ≠ every generated SQL** → `exemplars/` is a small hand-curated
   set (tens, not thousands). Never log runtime-generated SQL there; it's disposable.
5. **Don't over-share into `shared/`** → something goes there only when ≥2 packs want
   it identical. Persona and domain skills stay per-pack even if it means duplication.
6. **Cortex Agents / Genie are an alternative to the loop, not a base for it** →
   using them = handing them orchestration. The bridge: expose them AS a tool behind
   `SQLTool`, called BY the loop.
7. **Build the loop only when it earns its keep** → cross-domain questions, your own
   eval/guardrails, or multi-step planning. Single-domain BI Q&A is often better served
   by Cortex Analyst / Genie alone. Don't over-engineer.
8. **Two hosting shells, architecturally parallel** → deploy to whichever platform;
   the engine/shared/usecases are identical. Adding a platform = one more
   `platform_<name>/` folder.
9. **Accuracy-first: the loop must not degrade native retrieval** → we deliberately
   do NOT (a) rewrite/re-interpret the user's question before handing it to the
   native tool, nor (b) let the evaluator edit or regenerate SQL. Both risk making
   answers *less* accurate than the native agent (double-interpretation, ungrounded
   SQL edits). The loop's job is to critique and improve the *answer* over the data
   the native tool already returned — not to compete with the native tool's
   understanding or SQL generation. (See §12.)
10. **Independent worker + evaluator model selection** → each role is chosen by a
    *provider + model*, independently and symmetrically: worker = `WORKER_PROVIDER` /
    `WORKER_MODEL`, judge = `EVAL_PROVIDER` / `EVAL_MODEL`, with `auto` = the sensible
    default (evaluator defaults to the worker). Supports a different model on the same
    provider, or different providers entirely. A model grading its own output shares
    its blind spots and self-prefers; a different/stronger judge catches errors the
    worker was confident about. Implemented in `engine/llm_client.py`
    (`get_llm_client` / `get_eval_client` / `_resolve`) + `engine/graph.py`.
11. **Access is granted at the agent (deployment) level, like native** → one pack =
    one deployable service; grant the platform's RBAC on that service. We do NOT
    build our own per-user authorization layer for the single-agent case. A
    multi-agent gateway is the only thing that would require app-level authz. (See §12.)
12. **The judge returns a typed, validated verdict (plain Pydantic), not a regex scrape**
    → `evaluate` parses the judge reply into a `Verdict(score, reason)` and re-asks the
    judge on unparseable output (`eval_retries`), falling back to a safe score 0 if
    exhausted. Uses plain Pydantic (already a dependency) — we did NOT adopt an agent
    framework (Pydantic AI / instructor) for this; a framework in the generic core
    would violate "own the interface". (See §12.6.)
13. **Observability is a swappable sink, and we OWN the reasoning trail** → the platforms
    capture infra (endpoint I/O, tokens, the SQL a tool ran) but not the loop's decision
    trail (iterations, per-step score, judge reason). A `Tracer` interface
    (`engine/tracing.py`, `TRACER=stdout|mlflow|eventtable|none`) emits structured events;
    `StdoutTracer` (portable default, JSON to stdout) is collected by both platforms, with
    MLflow/event-table sinks stubbed. Token/cost telemetry is NOT rebuilt — read it from
    native usage/system tables and correlate by `run_id`. (See §12.7.)

---

## 7. What exists now (current state)

| Component | Status |
|---|---|
| Engine loop (LangGraph, generate→evaluate→refine, /18) | ✅ built, runs on mock |
| LLM adapters: mock ✅ · anthropic/cortex/databricks | ✅ mock; others stubbed (SDK shape + TODO) |
| SQL-tool adapters: mock ✅ · cortex-analyst/genie | ✅ mock; others stubbed |
| Shared tier (rubric, refine, reporting + sql_safety skills) | ✅ built |
| Use-case packs | ✅ `dq_qals`, `kpi_analytics`, `anomaly_rca`, `parity_hana_snowflake` |
| Exemplars (verified Q→SQL) + golden eval sets per pack | ✅ built, `run_evals.py` green (2/2 each) |
| Databricks shell (`platform_databricks/agent.py`) | ✅ MLflow ResponsesAgent recipe (class `PortableAgent`, use-case-neutral) |
| Snowflake shell (`platform_snowflake/`) | ✅ FastAPI + Dockerfile + spec.yaml, verified (/healthz 200, /invoke 18/18) |
| Independent worker + evaluator model selection | ✅ `WORKER_PROVIDER`/`WORKER_MODEL` + `EVAL_PROVIDER`/`EVAL_MODEL`, `auto` supported; mock verified |
| Hardened judge (typed `Verdict` + retry) | ✅ plain-Pydantic parse/validate, `eval_retries` re-ask, safe fallback; all 4 packs green |
| Observability (`Tracer` + structured events) | ✅ `engine/tracing.py`, `StdoutTracer` JSON events + `run_id`; mlflow/eventtable stubbed; mock verified |
| `CLAUDE.md` (Claude Code project memory) | ✅ built |

### The three analytics modes we built

| Pack | Question it answers | Rubric axes (each /6) |
|---|---|---|
| `dq_qals` | Is the data right? | rule correctness · evidence & quantification · actionability |
| `kpi_analytics` | What does the metric say? | metric-definition correctness · time grain & framing · clarity |
| `anomaly_rca` | Why did it change? | hypothesis quality · evidence & isolation · actionable conclusion |

---

## 8. Deployment targets

| Target | What it is | Fits when |
|---|---|---|
| **Databricks Apps** | Model Serving + MLflow ResponsesAgent | you're on Databricks |
| **Snowflake SPCS** | containers inside Snowflake | you're on Snowflake |
| **Any cloud container** | Cloud Run / ECS / AKS / your own k8s | multi-cloud, no vendor lock at all |
| **On-prem / VM** | plain Docker on a server you run | regulated, air-gapped, or an existing box |

Same engine every time; only the `platform_<name>/` shell changes.

---

## 9. What is intentionally NOT solved (scope honesty)

- **Semantic-layer portability** — rebuilt per platform, by design. Not abstracted here.
- **Real adapters** — Cortex/Databricks/Genie adapters are stubbed with the exact SDK
  shape + a `TODO`; not wired to live creds.
- **`spec.yaml` image URL** — placeholder `<repo_url>`; filled per Snowflake account.
- **Databricks catalog/schema names** — filled per workspace.

---

## 10. Open items / next steps

| Next step | Why it matters |
|---|---|
| Extract real Cortex verified queries → `exemplars/*.yaml` | Converts today's Snowflake-pooled tuning into portable git assets |
| Wire ONE real adapter end-to-end (e.g. Databricks Foundation Model) | Watch the loop run on a real model, not the mock |
| Add Streamlit-in-Snowflake chat UI | Gives the SPCS path a "looks like Genie" front end |
| Sync the GitHub remote | Remote lagged the local build (missing CLAUDE.md, 2 packs, platform_snowflake, etc.) |
| (If portability becomes board-level) neutral semantic layer via dbt/OSI | The only real lever for cross-platform semantic reuse |

---

## 11. How the pieces map to the goal

| Asset | Portable? | Where it lives |
|---|---|---|
| reasoning loop, adapters, eval runner, hosting shells | ✅ | `engine/` |
| base rubric, reporting/safety skills, config defaults | ✅ | `shared/` |
| persona, domain skills, verified Q→SQL, golden set | ✅ (owned in git) | `usecases/<name>/` |
| the platform shell | ▒ rewrite one small file to move | `engine/platform_<name>/` |
| Cortex / Genie / Databricks runtime | ░ swapped per vendor | (rented infra) |
| semantic model + raw data | ⚪ rebuilt per platform (accepted) | (native to each platform) |

**Bottom line:** everything that isn't the semantic layer is portable — and the
semantic layer we rebuild deliberately, the same way we migrate data.

---

## 12. Design exploration — accuracy-first, single-domain (session notes)

This section records an architecture exploration comparing this loop to **native
agents** (Snowflake Cortex Agents/Analyst, Databricks Genie). The frame throughout:
**single-domain is the target, and accuracy is the priority.** No cross-domain
ambition is required for the design to earn its keep.

### 12.1 Accuracy vs. a native agent — where it comes from

The loop calls the native text-to-SQL tool (`sql.ask` → Cortex Analyst / Genie).
So accuracy splits into two layers that behave differently:

| Layer | Who produces it | Loop vs. native |
|---|---|---|
| **Data layer** — is the SQL right, are the numbers correct | the native tool | **Identical** — same engine underneath; the loop can't out-retrieve it |
| **Answer layer** — does the write-up quantify / frame / conclude correctly | the loop's LLM + rubric | **Can be better** (rubric-enforced) **or worse** (bad rubric / self-grading) |

Mechanics confirmed in `engine/graph.py`: the SQL tool is called **once** in
`generate`; `refine` only rewrites the answer over the **same** extracted data; it
never re-queries. So the loop corrects *reasoning-over-data*, not *wrong-data*.

**Positioning (single-domain):** "same retrieval accuracy as the native agent, plus
a portable, governable answer-quality contract (rubric + golden-set evals) that
lives in git." The loop does **not** claim to beat native on raw SQL/number accuracy.

### 12.2 Deliberately rejected (accuracy-first)

Two capabilities were considered and **rejected** because they can *lower* accuracy:

- **Rewriting / re-interpreting the question before the native tool** — native has
  privileged grounding (semantic model synonyms, verified queries, column stats) our
  pre-processor doesn't see. Rewriting risks misaligning with the semantic model and
  a double-interpretation "telephone game." → **Not doing it.**
- **Evaluator editing or regenerating SQL** — loses semantic-model grounding, is
  dialect-specific (breaks portability), and lets a possibly-wrong judge overwrite a
  grounded query. → **Not doing it.** The SQL authority stays in the native tool.

What the evaluator **does** do (and this we keep): score the answer against the
rubric and drive a refine that produces **corrected insight from the same extracted
data** — fixing miscounts, missing quantification, weak framing, non-actionable
conclusions. Anything recoverable from the data in hand is fair game; anything that
would require different/again-run SQL is out of scope by choice.

### 12.3 Independent worker + evaluator models (implemented)

Worker and evaluator are each selected by a **provider + model**, independently and
symmetrically — so you can mix a different model under the same provider, or two
different providers. `auto` (or unset) = the sensible default. The judge being a
*different* model is what breaks the self-grading blind spot (a model shouldn't score
its own output); defaults keep them identical so existing runs are unchanged.

|        | provider            | model        | `auto`/unset resolves to |
|--------|---------------------|--------------|--------------------------|
| worker | `WORKER_PROVIDER`      | `WORKER_MODEL`  | `mock` / provider's default model |
| judge  | `EVAL_PROVIDER` | `EVAL_MODEL` | worker's provider / provider's default model |

Precedence per role: `WORKER_MODEL`/`EVAL_MODEL` > `<PROVIDER>_MODEL` > built-in default.

```
# same provider, different models
WORKER_PROVIDER=cortex  WORKER_MODEL=llama3.1-70b  EVAL_MODEL=claude-3-5-sonnet
# different providers
WORKER_PROVIDER=cortex  EVAL_PROVIDER=anthropic  EVAL_MODEL=claude-sonnet-4-5
```

Wiring: `engine/llm_client.py` → `get_llm_client()` / `get_eval_client()` (+ `_resolve`
for `auto`, `model_summary()` for the log); `engine/graph.py` → `evaluate` uses
`eval_llm`. A `[models] worker=<p>:<m>  evaluator=<p>:<m>` line prints at graph build.

### 12.4 Semantic layer — native, not ours

We leverage the **platform-native** semantic layer, we do **not** define our own:

- Snowflake: Cortex Analyst reads a **Semantic View / model YAML**
  (`CORTEX_SEMANTIC_MODEL=@stage/model.yaml` in `sql_tool.py`).
- Databricks: Genie reads **Unity Catalog** (metric views / instructions) via the
  Genie space (`GENIE_SPACE_ID`).

Consequence (already decision #2): the semantic model **does not port** — it is
rebuilt per platform, accepted like migrating data. What we own and carry across
platforms is the *tuning* (prompts, skills, rubric, verified Q→SQL exemplars, golden
evals), **not** the semantic model.

### 12.5 Access control across multiple single-domain agents

Native grants access **at the agent level** (RBAC on the Cortex service / Genie space
/ serving endpoint). This architecture mirrors that — no custom authz needed for the
single-agent case:

| Deployment shape | How access is granted | Trade-off |
|---|---|---|
| **One pack = one service** (recommended) | Deploy each pack as its own SPCS service / Databricks endpoint (`USE_CASE` picks the pack); grant the **platform's native RBAC** on that service — exactly like a native agent | Inherits native, per-agent access for free; more services to manage |
| **One multi-agent gateway** (supervisor over many packs) | A single endpoint serves several domains → you must build **app-level authz** mapping user → allowed packs | One entry point, but you now own an access layer native gives you |

Independent of the above, the **data layer stays governed by the warehouse**: the
native tool runs SQL under a role, so table / row / column access is enforced by
Snowflake / Databricks RBAC regardless of which agent is reached. (Deployment
decision to settle later: run queries with the **caller's** privileges so native
RBAC applies per-user, vs. the **service's** role which can over-grant.)

**Takeaway:** for the stated goal (single-domain agents, one per domain), grant
access the native way — one deployable service per pack — and reserve a gateway +
custom authz only if/when a true multi-domain supervisor is wanted.

### 12.6 Framework note (LangGraph vs. Pydantic AI) + hardened judge (implemented)

**Are they competitors?** Different layers. LangGraph = orchestration (the graph/loop);
Pydantic AI = typed agents + validated I/O + a model abstraction (with a smaller
`pydantic-graph` that *does* overlap LangGraph). For this repo:
- **Keep LangGraph** for the 3-node loop — no reason to switch.
- **Don't adopt Pydantic AI as the model/tool interface** — that would put a fast-moving
  framework inside the generic core, inverting our "own the interface" principle, and it
  doesn't even cover Cortex-via-Snowpark. If ever used, it belongs *behind* `LLMClient`.
- Pydantic AI is a **building block, not a portability strategy**; our value (pack
  composition, git-owned exemplars/evals, the `SQLTool` bridge, hosting shells) is
  orthogonal to it.

**What we DID take from that idea — the one real fit:** the judge's score used to be a
brittle regex scrape (`re.search(r"SCORE:\s*(\d+)")`) that would crash on any reply not
in the exact format. Hardened with **plain Pydantic** (already a dependency via
langgraph — no framework added):
- `Verdict(score: int, reason: str)` in `engine/graph.py`, with validation (score ≥ 0
  and ≤ threshold).
- `parse_verdict()` accepts either JSON `{"score","reason"}` or the house line form
  `SCORE: N/<max> - <reason>`; raises on anything unparseable/out-of-range.
- `evaluate` re-asks the judge on failure (`eval_retries`, default 1 → 2 tries total)
  with a corrective nudge, then falls back to a safe score 0 (forces a refine) instead
  of crashing. The `reason` is preserved into `feedback` so `refine` still gets guidance.
- Verified: retry recovers from transient garbage; exhaustion degrades gracefully; all
  four packs still 18/18 and evals green.

### 12.7 Observability — own the reasoning trail, rent the collection (implemented)

**Challenge to "the platform captures it for us":** half true. Model serving captures
*infra* — the endpoint's request/response, per-call tokens/cost, and the SQL a tool ran
(Cortex `QUERY_HISTORY` / usage views; Databricks inference tables / system tables). It
captures **nothing** about *this loop's* decisions: how many refine rounds, the score at
each step, the judge's reason, retries. That trail is the whole value here, and before
this change it went only to `print()` — and the Snowflake shell ran `verbose=False`, so
it was silently discarded. Extra wrinkle: if `WORKER_PROVIDER=anthropic` while hosting on
Snowflake, the LLM call leaves the container entirely, so the host sees no tokens at all.

**Solution — same pattern as everything else (own the interface, rent the backend):**

| Layer | What | Where |
|---|---|---|
| Portable core (own) | structured events per step via a `Tracer`; `StdoutTracer` = one JSON line each | `engine/tracing.py` + `engine/graph.py` |
| Native collection (rent) | Snowflake SPCS stdout → **event table** (`SYSTEM$GET_SERVICE_LOGS`); Databricks serving/**inference tables**, optional **MLflow Tracing** spans | per platform |
| Cost/token telemetry (native) | Cortex usage views / Databricks system tables — **not rebuilt**, read natively | per platform |

- `TRACER=stdout|otel|mlflow|eventtable|none`. Mature tools are **optional adapters, off
  by default** — you avoid the heavy tool until you actually want it:
  - **`stdout`** (default, zero deps): JSON events both platforms collect off stdout. Enough
    for most needs; no library, no backend.
  - **`otel`** (implemented): OpenTelemetry — run = one trace, each node = a child span; the
    root `run` span parents them (verified). Exports OTLP to `OTEL_EXPORTER_OTLP_ENDPOINT`,
    else a console exporter (no backend needed to see it). Lazy-imports `opentelemetry-sdk`
    only when selected; deps are commented-optional in `requirements.txt`. Vendor-neutral —
    the recommended path once a backend exists.
  - **`mlflow` / `eventtable`** (stubbed): Databricks-native / Snowflake-native sinks.
  Rule of thumb: don't adopt a heavy tool before you have a collector + a backend + someone
  who reads dashboards; until then `stdout` → event table / serving logs already answers
  "what did the loop do?" (matches decision #7).
- Events: `run_start · generate · evaluate · refine · run_end`, each carrying a
  per-invocation **`run_id`** plus per-node `ms`, score, best_score, answer preview, and
  the evaluate `verdict`. The Snowflake shell returns `run_id` from `/invoke`.
- **Correlation:** `run_id` is the join key between our loop events and the platform's SQL/
  token rows. (Future: also set it as a Snowflake query tag / Databricks request metadata.)

**Modular by construction (the key design point):** the loop (`graph.py`) contains NO
tracing logic — nodes are pure functions. All observability lives in `engine/tracing.py`
and is applied from the OUTSIDE: `instrument(name, fn, use_case)` wraps each node, and
`traced_invoke(graph, state, use_case)` brackets a run with `run_start`/`run_end`. To
change or extend observability you edit exactly one file. Same "own it or lose it" rule as
the rest of the repo: the reasoning trail is high-value + portable → we emit it; routine
billing telemetry is native → we read it, not rebuild it.

**Three guarantees (verified on mock, must not regress):**
1. **No accuracy / token / cost impact** — tracing never touches prompts, answers, scores,
   or control flow, and makes no model/SQL calls. Zero extra tokens.
2. **Zero overhead when disabled** — with `TRACER=none`, `instrument` returns the *bare*
   node function (no wrapper, no timing), and `traced_invoke` is a straight passthrough.
3. **Fail-safe** — the node/graph runs first; any tracer error (incl. a missing SDK like
   `mlflow`) is swallowed, so a broken sink can never break a run. Verified: `TRACER=mlflow`
   with mlflow absent still returns 18/18.
Latency when *enabled* is a `json.dumps` + stdout write per event (sub-ms vs. LLM calls);
real network-backed sinks should batch/flush async so this stays negligible.
