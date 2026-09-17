# AI gateway — LiteLLM, adopted as a dependency

We **adopt LiteLLM as the AI gateway**, exactly as we adopted **LangGraph as the loop engine**. We do
not build provider routing, fallback, retries, or key management — LiteLLM does. Our contribution is
**one thin adapter**.

| Concern | We adopt (dependency) | In our code | We do NOT build |
|---|---|---|---|
| Loop / orchestration | LangGraph | `engine/graph.py` | a custom state machine |
| Model gateway (routing / fallback / retries / keys) | **LiteLLM** | `engine/llm_client.py` (`LiteLLMClient`) | a custom router / fallback |

## One adapter, model-string routing
`LiteLLMClient.complete()` calls `litellm.completion(model=<model-string>, messages=[...])`. The
**model string is the routing key** — `anthropic/claude-sonnet-4-5`, `databricks/<serving-endpoint>`,
`openai/gpt-4o`, `azure/...`, `bedrock/...` — so one adapter reaches every provider LiteLLM supports.
Provider API keys come from the standard env vars LiteLLM reads (`ANTHROPIC_API_KEY`,
`OPENAI_API_KEY`, `DATABRICKS_*`, ...). `litellm` is imported lazily → the mock path never needs it.

## Two ways to run it (deployment choice, not a code change)
- **SDK, in-process (default):** `pip install litellm`; the adapter calls it directly. Zero infra;
  multi-provider + per-call fallbacks.
- **Proxy (opt-in, org governance):** run the LiteLLM Proxy (or Databricks AI Gateway — also
  OpenAI-compatible) and set `LITELLM_BASE_URL`. Central keys/budgets/rate-limits/logging live **in
  the proxy config**, not this repo.
  - **Routing caveat (M9):** `api_base` alone does **not** re-route a native model string. Setting
    `LITELLM_BASE_URL` while keeping `model: anthropic/claude-...` still calls Anthropic directly.
    To go *through* the proxy, use the proxy's route: `model: litellm_proxy/<name-your-proxy-exposes>`
    (or `openai/<name>` since the proxy is OpenAI-compatible) **with** `LITELLM_BASE_URL`. The proxy
    then maps that name to whatever backend/fallback it's configured for.

## Selecting it
```yaml
# usecases/<pack>/config.yaml  (optional; env overrides these)
models:
  worker:    { provider: litellm, model: "anthropic/claude-sonnet-4-5" }
  evaluator: { provider: litellm, model: "openai/gpt-4o" }
  planner:   { provider: litellm, model: "anthropic/claude-haiku-4-5" }   # OPTIONAL, planned packs only
```
Or via env (wins over pack config, preserving the one-env-var migration flip):
```bash
WORKER_PROVIDER=litellm  WORKER_MODEL=anthropic/claude-sonnet-4-5  python run_local.py <pack>
LITELLM_BASE_URL=http://localhost:4000   # optional: point at a proxy / Databricks AI Gateway
```
**Precedence:** env (`WORKER_PROVIDER`/`WORKER_MODEL`, `EVAL_*`, `PLANNER_*`) > pack `models:` >
built-in default. Three roles: the **worker** (frames, synthesizes, refines), the **evaluator** (judges;
only used when the pack sets `loop: true`), and the optional **planner** (`tool_mode: planned` only —
the structured plan call; unset = the worker plans). Secrets/base-url stay in **env**; a pack declares
only non-secret `provider + model`.

## The one exception: Cortex stays native
`CortexClient` keeps its own adapter — it authenticates via a Snowpark session **shared with
`CortexAnalystTool`** (which runs the generated SQL), which LiteLLM can't share. So the provider set
is: `mock` (zero-cred) · `cortex` (Snowflake-native) · `litellm` (everything else + any proxy). The
old `DatabricksClient` is superseded → prefer `provider: litellm, model: databricks/<endpoint>` (kept
for back-compat).

## Cortex through LiteLLM (optional) — two routes, one credential each
Native `provider: cortex` is the default for Cortex and needs only the PAT (it shares the Snowpark
session with Cortex Analyst). Routing Cortex *through* LiteLLM is optional and never needed for a PAT:
- **PAT via the OpenAI-compatible endpoint:** `provider: litellm, model: "openai/<cortex-model>"`,
  `LITELLM_BASE_URL=https://<account>.snowflakecomputing.com/api/v2/cortex/<openai-compatible-path>`,
  `OPENAI_API_KEY=<PAT>` (sent as the bearer). `LiteLLMClient` passes `api_base` through unchanged —
  no code change. *The exact OpenAI-compatible path must be confirmed live against your account.*
- **JWT / key-pair:** only for LiteLLM's native `snowflake/` provider — `provider: litellm,
  model: "snowflake/<model>"` with `SNOWFLAKE_JWT` + `SNOWFLAKE_ACCOUNT_ID`. Not required otherwise.
Either way LiteLLM-routed Cortex does **not** share the Snowpark session with `CortexAnalystTool`, so
keep native `provider: cortex` for data / AI+BI packs; use the LiteLLM route only where you want one
gateway for a non-data agent or central governance.

## Memory seam (later)
Per-user/tier routing (premium user → stronger model) reads a per-request profile into `models:` /
the LiteLLM Router; needs identity + memory, so out of scope now.
