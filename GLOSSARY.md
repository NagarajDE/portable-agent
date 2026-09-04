# Glossary — plain-language terms for this project

Short, plain explanations of the terms used across this repo. Each is 1–2 lines and
tied to *how we use it here*, not textbook definitions. For the full story see
`PROJECT_CONTEXT.md`; for the operational spec see `CLAUDE.md`.

## Core building blocks

**Interface** — an agreed "shape" for talking to something, without caring how it's
built inside. It's a promise, not working code.
*Example:* our `LLMClient` interface just says *"whatever goes here must have a
`.complete(prompt) → text` method."* The loop relies on that promise and stays the same
no matter which vendor fulfills it.
*Analogy:* the shape of a wall socket — a standard that many different plugs can fit.

**LLMClient** — the interface (the "slot") for *the AI model*. By itself it can't talk to
anything; it just defines the shape every model must fit. (Code: `engine/llm_client.py`.)

**Adapter** — a real class that *fills* an interface's slot for **one specific vendor**.
One interface, many adapters. An adapter is the **only** place a vendor's SDK is imported.
*Examples (all are `LLMClient` adapters):*
- `MockClient` — fake, returns canned text (no vendor).
- `AnthropicClient` — calls the Anthropic SDK.
- `CortexClient` — runs a Snowflake Cortex SQL call.
- `DatabricksClient` — calls a Databricks serving endpoint.
*Analogy:* the different plugs (Claude plug, Snowflake plug, mock plug) that all fit the
same socket. The loop "plugs in" and never knows which plug is in — swapping vendors =
swapping the plug; the socket and everything wired to it is unchanged.

**LLMClient vs. Adapter in one line:** `LLMClient` is the *shape*; `AnthropicClient`,
`CortexClient`, etc. are the *things that fit the shape*. Each adapter "is an" LLMClient.

**SQLTool** — the same interface idea, but for *fetching data*: its promise is
*"has an `.ask(question) → rows` method."* Its adapters are `MockSQLTool`,
`CortexAnalystTool` (Snowflake), and `GenieTool` (Databricks). (Code: `engine/sql_tool.py`.)

**Tracer** — the same interface idea again, but for *observability*: its promise is
*"has an `.event(name, **fields)` method."* Adapters: `StdoutTracer` (JSON line — the
zero-dep default), `OTelTracer` (OpenTelemetry), `MLflowTracer` (Databricks),
`EventTableTracer` (Snowflake). Chosen with `TRACER=stdout|otel|mlflow|eventtable|none`.
The mature tools are *optional* — off unless you switch to them. (Code: `engine/tracing.py`.)

**OpenTelemetry (OTel)** — an open, vendor-neutral industry standard for traces/logs, with
an "export anywhere" model. Our `OTelTracer` emits it; you point it at any backend. Choosing
it is not lock-in — that's the whole appeal.

**Span / Trace** — OTel's units. A *trace* is one whole run; a *span* is one step inside it
(here: the `run` span is the trace root, and each `generate`/`evaluate`/`refine` is a child
span with its own duration). All spans of a run share one trace id.

## The three tiers

**Engine** — the generic, reusable core (`engine/` folder): the loop and the interfaces.
Written once; it doesn't change when you add a new agent or switch platforms.

**Shared** — common conventions every agent inherits (`shared/` folder): the base rubric,
the reporting/safety skills, default settings. Put something here only if ≥2 agents want
it identical.

**Use-case pack** — one folder = one agent (e.g. `usecases/dq_qals/`). Holds just that
agent's personality and tuning; everything mechanical is inherited from engine + shared.
Adding an agent = adding a folder.

## How an agent runs

**The loop (generate → evaluate → refine)** — draft an answer, have a judge score it
against the rubric, improve it, repeat until it's good enough. That's the whole agent.
*Example run:* draft scores 12/18 → refine → 14 → 16 → 18, stop.

**Worker vs. evaluator (judge)** — the worker AI writes the answer; the evaluator AI
grades it. We allow them to be different models so a model isn't grading its own work.

**Rubric** — the scorecard (out of 18) the judge uses to decide if an answer is good.
*Example (dq_qals):* rule correctness /6 · evidence & quantification /6 · actionability /6.

**Verdict** — the judge's validated result: a score plus a short reason. We parse it into
a typed object (not a fragile text scrape) and re-ask the judge if it comes back garbled.

**Threshold / max_iters** — "good enough" score to stop at (18), and the most refine
rounds to try before giving up.

## Assets you own (the portable tuning)

**Persona** — the one-paragraph instructions that give an agent its voice and job
(`prompts/generate.md`). The single file that's truly per-agent.

**Skills** — reusable how-to notes (Markdown) the agent is given, e.g. duplicate
detection or SQL-safety rules.

**Exemplars** — a small, hand-picked set of verified *question → SQL* pairs used as
examples. Curated (tens, not thousands), and owned in git so they don't get locked
inside a vendor.
*Example pair (from `dq_qals`):* Q "Find duplicate inspection lots in QALS" → SQL
`SELECT PRUEFLOS, COUNT(*) AS N FROM QALS GROUP BY PRUEFLOS HAVING COUNT(*) > 1`.

**Evals / golden set** — a fixed list of test questions with expected outcomes, so you
can prove a change didn't break anything (`run_evals.py`).

## Platforms & running it

**Mock** — a fake, credential-free stand-in for the real model/database, so the whole
thing runs on your laptop with nothing connected. The default.

**Provider** — which backend actually serves a request: `mock`, `anthropic`, `cortex`
(Snowflake), or `databricks`. Chosen per role with `WORKER_PROVIDER` / `EVAL_PROVIDER`.

**Hosting shell** — the thin per-platform wrapper that exposes the same loop in a
platform's expected form: `platform_databricks/` (MLflow) or `platform_snowflake/`
(a web service). The only file you rewrite to move platforms.

**Semantic layer** — the platform-native definition of your tables/metrics (Snowflake
Semantic View, Databricks Metric View) that the SQLTool reads. It is *native, not ours*,
and is rebuilt per platform.

**Portability** — the whole point: your loop, prompts, rubric, exemplars, and evals move
between Snowflake and Databricks unchanged; only the adapter and semantic layer are
swapped/rebuilt.

## Observability

**Observability** — being able to see *what the agent did and why*: how many refine
rounds, the score at each step, the judge's reason, how long each step took.

**Event** — one structured record emitted at each step (`run_start`, `generate`,
`evaluate`, `refine`, `run_end`), e.g. `{"event":"evaluate","score":14,"verdict":"..."}`.
Emitted by the tracing module wrapping the loop from the outside — the loop code itself
has no logging in it.

**instrument / traced_invoke** — the two hooks (in `engine/tracing.py`) that add
observability without touching the loop: `instrument` wraps one node to emit a timed
event; `traced_invoke` wraps a whole run with start/end events. When `TRACER=none` they
add nothing (the node stays the bare function).

**run_id** — a short unique id per question, stamped on every event (and returned by the
Snowflake `/invoke`). It's the "join key" that links our loop events to the platform's
own SQL/token records for the same request.

**Infra logs vs. reasoning trail** — the platform auto-captures *infra* (endpoint in/out,
tokens, the SQL that ran); it does **not** capture the *reasoning trail* (iterations,
scores, judge reasons). We emit the trail ourselves via the Tracer.

**Event table (Snowflake)** — a Snowflake table that collects a service's stdout/logs, so
our JSON events become queryable rows.

**Inference table (Databricks)** — a Databricks table that auto-logs a serving endpoint's
requests and responses.
