# Run the agent locally on Windows against real Snowflake / Databricks

Test the loop **on your laptop**, hitting a real platform — no container, no cloud deploy.
Same `engine/` + packs as production; only the auth differs (locally you use your own
credentials; inside SPCS / Model Serving the platform injects a token).

- **Snowflake + Cortex** — fully wired today. This is the path that works end to end. §1–§3.
- **Databricks + Genie** — the adapters are **stubbed**. You can test the *loop* locally, but
  hitting real Databricks model serving + Genie needs ~two functions wired first. §4 is honest
  about exactly what and how.

> This is a superset of `docs/deployment/deploy-snowflake.md` **Appendix A**, written
> Windows-first and adding the **existing Semantic View** path. If you only want the Snowflake
> quickstart, Appendix A is the short version.

---

## 0. One-time setup (Windows / PowerShell)

From the **repo root** in PowerShell:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1          # if blocked: Set-ExecutionPolicy -Scope Process RemoteSigned
py -3 -m pip install langgraph pyyaml python-dotenv
```

- `python-dotenv` lets `run_local.py` read a `.env` file automatically, so you don't have to
  `set` a dozen env vars in the shell each time.
- Provider SDKs are **lazy** — install only the one you use:
  - Snowflake: `py -3 -m pip install snowflake-snowpark-python requests`
  - Anthropic (handy for isolating the LLM half): `py -3 -m pip install anthropic`

Create your `.env` (it is **git-ignored — never committed**):

```powershell
Copy-Item .env.example .env
```

Sanity check with the built-in mock (touches nothing external — proves the code runs):

```powershell
py -3 run_local.py dq_qals
```

You should see the loop print a climbing score ending in `BEST SCORE : 18/18`.

---

## 1. What you need from Snowflake (one time)

- **Cortex enabled**, and a role granted the `SNOWFLAKE.CORTEX_USER` database role.
- A **warehouse** you can use, and **SELECT** on the tables your questions touch.
- A **Programmatic Access Token (PAT)** for the Cortex Analyst REST call. Snowsight →
  *your user → Settings → Authentication → Programmatic access tokens*, scoped to your role.
- A **semantic layer** for Cortex Analyst — **one** of:
  - an **existing native Semantic View** (an object in your account), **or**
  - a **semantic model YAML** uploaded to a stage.

  You already have Semantic Views, so use those — see §2.

---

## 2. Two ways to point Cortex at your semantic layer

Cortex Analyst turns the question into SQL using a semantic layer. The adapter
(`engine/sql_tool.py` → `CortexAnalystTool`) accepts **either** form; set exactly one in
`.env` (if both are set, the **view wins**):

| You have… | Set in `.env` | REST field used |
|---|---|---|
| an existing **Semantic View** (native object) | `CORTEX_SEMANTIC_VIEW=MY_DB.MY_SCHEMA.MY_VIEW` | `semantic_view` |
| a semantic model **YAML on a stage** | `CORTEX_SEMANTIC_MODEL=@MY_DB.MY_SCHEMA.MY_STAGE/model.yaml` | `semantic_model_file` |

> **Find your Semantic Views:** in Snowsight run `SHOW SEMANTIC VIEWS;` (or look under
> *AI & ML → Cortex Analyst*). Use the fully-qualified `DB.SCHEMA.NAME`.
>
> **Caveat (decision #2 in `CLAUDE.md`):** a Snowflake Semantic View does **not** port to
> Databricks — its Metric Views are a different, native format. Pointing at an existing view
> here is the right call for *Snowflake*; the portable, cross-platform assets are still your
> `usecases/*/exemplars` (verified `Q→SQL`) and `evals` (golden questions).

---

## 3. Fill `.env` and run — in two steps (so failures are easy to pin down)

Open `.env` and set (leave everything else as-is):

```dotenv
WORKER_PROVIDER=cortex
EVAL_PROVIDER=auto            # judge = same provider as worker; or set anthropic to cross-check
SQL_TOOL=cortex

SNOWFLAKE_ACCOUNT=ab12345.us-east-1          # your account identifier
SNOWFLAKE_USER=YOUR_USER
SNOWFLAKE_PASSWORD=YOUR_PASSWORD
SNOWFLAKE_ROLE=YOUR_ROLE                      # must have SNOWFLAKE.CORTEX_USER
SNOWFLAKE_WAREHOUSE=YOUR_WH                   # runs COMPLETE + the Analyst-generated SQL
SNOWFLAKE_DATABASE=YOUR_DB
SNOWFLAKE_SCHEMA=YOUR_SCHEMA

# Cortex Analyst REST call:
SNOWFLAKE_HOST=ab12345.us-east-1.snowflakecomputing.com
SNOWFLAKE_PAT=<your programmatic access token>

# Semantic layer -- pick ONE (see §2):
CORTEX_SEMANTIC_VIEW=MY_DB.MY_SCHEMA.MY_VIEW
# CORTEX_SEMANTIC_MODEL=@MY_DB.MY_SCHEMA.MY_STAGE/model.yaml

# optional:
# CORTEX_MODEL=claude-3-5-sonnet             # the Cortex COMPLETE model the worker/judge use
```

### Step 1 — test Cortex COMPLETE only (the LLM half)

Temporarily set `SQL_TOOL=mock`, then:

```powershell
py -3 run_local.py dq_qals
```

A climbing score means **Cortex `COMPLETE` + your credentials work**. This isolates the LLM
from the SQL, so if it fails you know it's auth/COMPLETE, not Analyst.

### Step 2 — add Cortex Analyst (the SQL half)

Set `SQL_TOOL=cortex`, then run again:

```powershell
py -3 run_local.py dq_qals
```

Now the loop calls Cortex Analyst (REST) to turn the question into SQL against **your Semantic
View**, runs that SQL on your warehouse, and answers from the **real rows**.

### Try your own question / the offline eval gate

```powershell
py -3 run_evals.py dq_qals      # runs the pack's golden questions through the full loop
```

### What breaks (and the fix)

| Symptom | Cause → fix |
|---|---|
| `No Snowflake token…` | `SQL_TOOL=cortex` but no `SNOWFLAKE_PAT` (+`SNOWFLAKE_HOST`). Add them. |
| `KeyError: 'SNOWFLAKE_PASSWORD'` (or account/user/…) | a required `SNOWFLAKE_*` value is missing from `.env`. |
| `SQL_TOOL=cortex needs a semantic layer…` | set `CORTEX_SEMANTIC_VIEW` **or** `CORTEX_SEMANTIC_MODEL`. |
| HTTP **401/403** on the Analyst call | PAT invalid/expired, or the role lacks Cortex. Regenerate the PAT; confirm `SNOWFLAKE.CORTEX_USER`. |
| HTTP **400** naming the semantic model/view | the view name/stage path is wrong or the role can't see it. Re-check `SHOW SEMANTIC VIEWS;`. |
| SQL run error (table not found / no access) | the role can't SELECT the tables behind the view. Grant SELECT. |

> Don't want to use a password locally? You can key-pair auth instead, but the shipped adapter
> reads `SNOWFLAKE_PASSWORD`; wiring key-pair is a small change in `snowpark_session()`
> (`engine/llm_client.py`). Ask if you want that.

---

## 4. Databricks locally — honest status

The Databricks worker (`DatabricksClient`) and Genie SQL tool (`GenieTool`) are **stubbed**
(`raise NotImplementedError` + a `TODO`; see the "Adapter status" note in `CLAUDE.md`). So:

### 4a. What works **today** — test the loop with a real LLM, mock SQL

Use Anthropic (or Cortex) as the worker while the SQL stays mock. This exercises the whole
generate→evaluate→refine loop against a real model on Windows:

```dotenv
WORKER_PROVIDER=anthropic
WORKER_MODEL=claude-sonnet-4-5
SQL_TOOL=mock
# ANTHROPIC_API_KEY=sk-ant-...
```
```powershell
py -3 run_local.py dq_qals
```

### 4b. What it takes to hit **real** Databricks + Genie

Two functions to wire (mirror the working Cortex adapters), each ~20 lines:

1. `engine/llm_client.py` → `DatabricksClient.complete()` — call your Model Serving /
   Foundation Model endpoint (the SDK shape + `TODO` are already there).
2. `engine/sql_tool.py` → `GenieTool` — start/continue a Genie conversation, run the returned
   SQL (mirror `CortexAnalystTool`; run it through `_ensure_read_only()`).

Then the local pattern is identical to Snowflake — `.env`:

```dotenv
WORKER_PROVIDER=databricks
SQL_TOOL=genie
DATABRICKS_HOST=https://<your-workspace-host>
DATABRICKS_TOKEN=dapi...
GENIE_SPACE_ID=01ef...
```
```powershell
py -3 run_local.py dq_qals
```

Tip: wire and test **one half at a time** — `WORKER_PROVIDER=databricks` with `SQL_TOOL=mock`
first (isolates the LLM), then flip `SQL_TOOL=genie`. Same isolation logic as §3.

> Semantic layer note: on Databricks the equivalent of a Semantic View is a **Metric View**,
> and Genie is pointed at it via the Genie space — not an env var. It does not share a format
> with Snowflake's; that's expected (decision #2).

---

## 5. Optional: persist runs + feedback while you test (the flywheel)

Off by default. Turn it on to capture every run (and any 👍/👎) into append-only rows you own —
the raw material you later curate into `exemplars` + `evals`
(see [`concepts/evals-and-the-learning-flywheel.md`](concepts/evals-and-the-learning-flywheel.md)):

```dotenv
MEMORY_STORE=sqlite                       # writes a local file; no Snowflake needed
# MEMORY_SQLITE_PATH=portable_agent_memory.db
```

Use `MEMORY_STORE=snowflake` to write to `PORTABLE_AGENT_INTERACTIONS` / `_FEEDBACK` with your
`SNOWFLAKE_*` creds instead.

---

## See also

- `docs/deployment/deploy-snowflake.md` — containerize + deploy to SPCS (Appendix A = the short local test).
- `docs/deployment/deploy-databricks.md` — MLflow Models-from-Code deploy.
- `docs/concepts/evals-and-the-learning-flywheel.md` — how logged runs become git-owned tests/exemplars.
- `CLAUDE.md` — adapter status, the env-var matrix, and the core principle.
