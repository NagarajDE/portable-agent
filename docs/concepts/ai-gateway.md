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
  the proxy config**, not this repo. Same adapter, one env var.

## Selecting it
```yaml
# usecases/<pack>/config.yaml  (optional; env overrides these)
models:
  worker:    { provider: litellm, model: "anthropic/claude-sonnet-4-5" }
  evaluator: { provider: litellm, model: "openai/gpt-4o" }
```
Or via env (wins over pack config, preserving the one-env-var migration flip):
```bash
WORKER_PROVIDER=litellm  WORKER_MODEL=anthropic/claude-sonnet-4-5  python run_local.py <pack>
LITELLM_BASE_URL=http://localhost:4000   # optional: point at a proxy / Databricks AI Gateway
```
**Precedence:** env (`WORKER_PROVIDER`/`WORKER_MODEL` …) > pack `models:` > built-in default.
Secrets/base-url stay in **env**; a pack declares only non-secret `provider + model`.

## The one exception: Cortex stays native
`CortexClient` keeps its own adapter — it authenticates via a Snowpark session **shared with
`CortexAnalystTool`** (which runs the generated SQL), which LiteLLM can't share. So the provider set
is: `mock` (zero-cred) · `cortex` (Snowflake-native) · `litellm` (everything else + any proxy). The
old `DatabricksClient` is superseded → prefer `provider: litellm, model: databricks/<endpoint>` (kept
for back-compat).

## Memory seam (later)
Per-user/tier routing (premium user → stronger model) reads a per-request profile into `models:` /
the LiteLLM Router; needs identity + memory, so out of scope now.
