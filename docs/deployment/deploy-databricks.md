# Deploy to Databricks — step by step for a first-timer

This gets the agent running as a **Model Serving endpoint** on Databricks, using
**MLflow "Models-from-Code"** + the **`databricks-agents`** helper. No prior MLflow
experience assumed.

> **We deploy on MOCK first.** The shipped Databricks/Genie adapters are stubbed, so the
> first deploy runs the built-in MOCK provider — no external calls, guaranteed to work.
> Once you see it serving, the last section shows how to switch to the real Databricks
> Foundation Model API + Genie. Do the mock deploy first.

---

## 0. The 60-second mental model

| Term | What it is (plain) |
|---|---|
| **MLflow** | Databricks' built-in tool for packaging + tracking models. |
| **Models-from-Code** | Instead of pickling an object, you point MLflow at a `.py` file that builds the agent. Ours is [`engine/platform_databricks/agent.py`](../../engine/platform_databricks/agent.py). |
| **Unity Catalog (UC)** | Databricks' governance layer. Your model is registered at `catalog.schema.name`. |
| **Model Serving endpoint** | A managed REST endpoint that runs your model. |
| **`databricks-agents`** | A helper (`agents.deploy(...)`) that wraps "register + serve" into one call and gives you a review/chat UI. |

Flow: **get the code into the workspace → run a notebook that logs the model → register to UC → `agents.deploy()` → query the endpoint.**

---

## 1. Prerequisites

- A Databricks workspace with **Unity Catalog enabled**.
- A **catalog + schema** you can create models in (this guide uses `main.dq` — replace with
  yours). You need `CREATE MODEL` on the schema and permission to create serving endpoints.
- Ability to run a **notebook** on a cluster (or serverless).

---

## 2. Get the code into the workspace

Easiest: add this repo as a **Git folder**.

1. In Databricks: **Workspace → (your folder) → Create → Git folder**.
2. Paste the repo's HTTPS URL, clone it. You now have `engine/`, `shared/`, `usecases/` in
   the workspace.
3. Create a **new notebook inside the cloned repo folder** (so the relative paths
   `engine/…` resolve). Name it e.g. `deploy_dbx`.

*(Alternative: upload the folders manually — but the Git folder keeps paths correct with no effort.)*

---

## 3. The deploy notebook (copy these cells)

**Cell 1 — install deps and restart Python:**
```python
%pip install -r requirements.txt -r requirements-optional.txt   # pinned; optional adds mlflow + databricks-agents
dbutils.library.restartPython()
```

**Cell 2 — log, register, and deploy on MOCK:**
```python
import mlflow
from databricks import agents

CATALOG, SCHEMA, NAME = "main", "dq", "dq_agent"     # <-- change to your UC catalog/schema
UC_MODEL = f"{CATALOG}.{SCHEMA}.{NAME}"

with mlflow.start_run():
    info = mlflow.pyfunc.log_model(
        name="dq_agent",
        python_model="engine/platform_databricks/agent.py",   # Models-from-Code
        code_paths=["engine", "shared", "usecases"],           # ship all three tiers
        pip_requirements=["langgraph", "pyyaml", "pydantic", "openai",
                          "mlflow", "databricks-agents", "databricks-sdk"],
    )

mlflow.set_registry_uri("databricks-uc")
mlflow.register_model(info.model_uri, UC_MODEL)

# Deploy to a Model Serving endpoint. environment_vars run the endpoint on MOCK first:
agents.deploy(
    UC_MODEL, version=1,
    environment_vars={
        "USE_CASE": "dq_qals",
        "WORKER_PROVIDER": "mock",
        "SQL_TOOL": "mock",
        "TRACER": "stdout",       # JSON trace events -> serving logs
    },
)
```

Run both cells. `agents.deploy(...)` takes a few minutes to spin up the endpoint. When it
finishes it prints the endpoint name/URL and a **Review App** link.

> **Why `environment_vars`?** The serving container starts fresh; these tell it to run the
> MOCK provider. Without them the code defaults to `databricks`/`genie`, which are stubbed
> and would error. So set them for the mock deploy.

---

## 4. Query the endpoint

The model is an MLflow **`ResponsesAgent`**, so the input is a messages list.

- **UI:** open the endpoint in **Serving → your endpoint → Query**, and send:
  ```json
  {"input": [{"role": "user", "content": "Any duplicate inspection lots in QALS today?"}]}
  ```
  You should get an assistant message back with the mock DQ answer.

- **Python (SDK):**
  ```python
  from databricks.sdk import WorkspaceClient
  w = WorkspaceClient()
  r = w.serving_endpoints.query(
      "dq_agent",
      dataframe_records=[{"input": [{"role": "user", "content": "duplicate lots?"}]}],
  )
  print(r.predictions)
  ```

*(Exact query helper differs slightly by SDK version — the **Query** UI panel always shows a
working example for your endpoint. Use that if the SDK call shape drifts.)*

---

## 5. A chat UI (optional)

- The **Review App** link from `agents.deploy(...)` is already a chat UI you can share.
- For a branded app, build a small **Databricks App** that calls the serving endpoint.
- Or use the **AI Playground** to poke at the endpoint interactively.

---

## 6. Everyday commands

- **Redeploy after a code change:** re-run Cell 2 (it logs a new version; bump `version=` in
  `agents.deploy` or deploy the latest registered version).
- **Manage the endpoint:** Databricks UI → **Serving** → your endpoint (scale to zero, view
  logs, delete).
- **Logs / observability:** the serving endpoint's logs contain our JSON trace events
  (`run_start`/`generate`/`evaluate`/`refine`/`run_end`) with a `run_id`. For richer traces
  you can later set `TRACER=mlflow` (adapter stubbed) or `TRACER=otel`.

---

## 7. Switch from MOCK to real (later)

Both Databricks adapters are **wired**: `DatabricksClient` (Foundation Model API, OpenAI-compatible;
or prefer `provider: litellm, model: databricks/<endpoint>`) and `GenieTool` (`SQL_TOOL=genie`: Genie
Conversation API → the generated SQL is captured + read-only-checked → Genie's query result is the
evidence; zero rows / a clarification → the `NO_DATA` sentinel, exactly like Cortex Analyst).

1. **Declare the Genie space in the PACK** (functional config lives in the pack, never in env):
   ```yaml
   # usecases/<pack>/semantic_layer.yaml
   databricks:
     genie_space: 01ef1234abcd5678          # REQUIRED for SQL_TOOL=genie
     metric_view: main.gold.my_metric_view  # OPTIONAL: enables term->value binding (needs a warehouse id)
   ```
2. **Provide the endpoint the credentials**, via `environment_vars` on `agents.deploy(...)`:
   ```python
   environment_vars={
       "USE_CASE": "<pack>",
       "WORKER_PROVIDER": "databricks", "WORKER_MODEL": "<serving-endpoint>",   # or litellm + databricks/<endpoint>
       "SQL_TOOL": "genie",
       "DATABRICKS_HOST": "https://<your-workspace-host>",
       "DATABRICKS_TOKEN": "{{secrets/<scope>/<key>}}",   # use a Databricks secret, not a literal
       "DATABRICKS_WAREHOUSE_ID": "<sql warehouse id>",   # OPTIONAL: only for metric-view value binding
       "GENIE_TIMEOUT_SECONDS": "120",                    # OPTIONAL: Genie plans + runs the SQL
   }
   ```
   (Store the token in a **Databricks secret scope**; don't paste it in the notebook. The token needs
   CAN RUN on the Genie space and SELECT on its tables — Genie runs queries as the caller.)
3. Re-run the deploy cell. Tip: test with `WORKER_PROVIDER=databricks` + `SQL_TOOL=mock`
   first to isolate the LLM path from the Genie path.

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `python_model` path not found | Run the notebook from **inside the cloned repo folder** so `engine/…` resolves. |
| `ModuleNotFoundError` at serving | A dep missing from `pip_requirements` — keep the list above (incl. `pydantic`). |
| Endpoint `READY` but query errors | You deployed without the mock `environment_vars`, so it defaulted to the live `databricks`/`genie` adapters without a model / token / Genie space. Redeploy with the mock env from step 3, or configure §7. |
| UC register fails | Wrong catalog/schema or missing `CREATE MODEL`; ask your workspace admin. |
| Don't know the query format | Use the endpoint's **Query** UI panel — it shows a working example. |
