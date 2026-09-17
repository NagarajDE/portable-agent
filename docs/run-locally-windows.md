# Run the agent locally on Windows against real Snowflake / Databricks

Test the loop **on your laptop**, hitting a real platform — no container, no cloud deploy.
Same `engine/` + packs as production; only the auth differs (locally you use your own
credentials; inside SPCS / Model Serving the platform injects a token).

- **Snowflake + Cortex** — fully wired and live-verified. §1–§3.
- **Databricks + Genie** — both adapters are **wired** (`DatabricksClient` / `provider: litellm` for the
  model, `GenieTool` for `SQL_TOOL=genie`); the Genie path is unit-tested against a fake SDK client and
  awaits a live run. §4.

> This is a superset of `docs/deployment/deploy-snowflake.md` **Appendix A**, written
> Windows-first and adding the **existing Semantic View** path. If you only want the Snowflake
> quickstart, Appendix A is the short version.

---

## 0. One-time setup (Windows / PowerShell)

From the **repo root** in PowerShell:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1          # if blocked: Set-ExecutionPolicy -Scope Process RemoteSigned
py -3 -m pip install -r requirements.lock.txt
```

- Install the **lock**, not the intent file: `requirements.txt` pins what we import, `requirements.lock.txt`
  pins **every transitive** too (universal: any platform, Python ≥ 3.11). Into an *existing* environment
  this also upgrades a transitive that an older install left behind — a real case: `starlette` (which serves
  the HTTP endpoint) sat at a version with five advisories because `fastapi` only bounds it `>=0.46`. Don't
  hand-pick packages. See [`docs/concepts/security-and-telemetry.md`](concepts/security-and-telemetry.md) §3.
- `python-dotenv` (included) lets `run_local.py` read a `.env` file automatically, so you don't have to
  `set` a dozen env vars in the shell each time.
- Provider SDKs are **lazy-imported**, so an unused provider costs nothing at runtime. The default file
  already carries Cortex/Snowpark, the LiteLLM gateway, `openai` and `databricks-sdk`. The opt-in ones —
  direct `anthropic`, OpenTelemetry, the Databricks deploy stack — are in `requirements-optional.txt`:
  `py -3 -m pip install -r requirements-optional.txt` (or just the one line you need).

Create your `.env` (it is **git-ignored — never committed**):

```powershell
Copy-Item .env.example .env
```

Sanity check with the built-in mock (touches nothing external — proves the code runs):

```powershell
py -3 run_local.py dq_qals
```

You should see the loop print a climbing score ending in `BEST SCORE : 18/18`.

If retrieval came back empty, you get a **reason instead of a score** — the run was escalated, not
answered, and it is never scored:

```
RESULT     : NO USABLE DATA — the query ran and matched nothing (escalated, not scored — retries: 1)
RESULT     : OUT OF SCOPE — this data cannot answer that question (escalated, not scored — retries: 1)
```

`no_data` means check the identifier / filters / freshness; `out_of_scope` means this pack's data can't
answer that question at all. Both still print the agent's honest explanation as `MESSAGE`. See
[concepts/retries-explained.md](concepts/retries-explained.md).

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

## 2. Point a pack at your semantic layer

Cortex Analyst turns the question into SQL using a semantic layer. An AI+BI/Analyst pack **declares
which one it uses** in its own `usecases/<pack>/semantic_layer.yaml` — read by `engine/`, never from
env. Set exactly one form under the `snowflake:` block (if both are set, the **view wins**):

```yaml
# usecases/<pack>/semantic_layer.yaml
snowflake:
  view: MY_DB.MY_SCHEMA.MY_VIEW                          # a native Semantic View  -> REST field semantic_view
  # model_file: "@MY_DB.MY_SCHEMA.MY_STAGE/model.yaml"   # OR a stage YAML         -> REST field semantic_model_file
```

> **Find your Semantic Views:** in Snowsight run `SHOW SEMANTIC VIEWS;` (or look under
> *AI & ML → Cortex Analyst*). Use the fully-qualified `DB.SCHEMA.NAME`.
>
> **Test-only override:** to point a *local run* at a DIFFERENT layer than the pack declares (or to
> give a demo pack that ships no `semantic_layer.yaml`, e.g. `dq_qals`, a view), set
> `CORTEX_SEMANTIC_VIEW` (or `CORTEX_SEMANTIC_MODEL`) in `.env`. These are read ONLY by the runner
> scripts (`run_local.py` / `run_evals.py` / `chat_local.py`), never by `engine/`. Leave them unset
> to use the pack's own `semantic_layer.yaml`.
>
> **Caveat (decision #2 in `CLAUDE.md`):** a Snowflake Semantic View does **not** port to
> Databricks — its Metric Views are a different, native format. Pointing at an existing view
> here is the right call for *Snowflake*; the portable, cross-platform assets are still your
> `usecases/*/exemplars` (verified `Q→SQL`) and `evals` (golden questions).

---

## 3. Fill `.env` and run — in two steps (so failures are easy to pin down)

Open `.env` and set (leave everything else as-is):

**One secret does it all:** a single `SNOWFLAKE_PAT` authenticates *both* the Snowpark session
(COMPLETE + running the SQL) and the Cortex Analyst REST call — there is **no** `SNOWFLAKE_PASSWORD`.

```dotenv
WORKER_PROVIDER=cortex
EVAL_PROVIDER=auto            # judge = same provider as worker; or set anthropic to cross-check
SQL_TOOL=cortex

SNOWFLAKE_ACCOUNT=ab12345.us-east-1          # your account identifier
SNOWFLAKE_USER=YOUR_USER
SNOWFLAKE_PAT=<your programmatic access token>   # the ONLY secret — used as password AND REST bearer
SNOWFLAKE_HOST=ab12345.us-east-1.snowflakecomputing.com   # for the Analyst REST call
SNOWFLAKE_ROLE=YOUR_ROLE                      # must have SNOWFLAKE.CORTEX_USER; PAT should be scoped to it
SNOWFLAKE_WAREHOUSE=YOUR_WH                   # runs COMPLETE + the Analyst-generated SQL
SNOWFLAKE_DATABASE=YOUR_DB
SNOWFLAKE_SCHEMA=YOUR_SCHEMA

# Semantic layer: an AI+BI pack declares it in usecases/<pack>/semantic_layer.yaml (see §2).
# The vars below are an OPTIONAL TEST-ONLY override -- used to point a demo pack (e.g. dq_qals, which
# ships no semantic_layer.yaml) at a view. Leave unset for packs that declare their own:
CORTEX_SEMANTIC_VIEW=MY_DB.MY_SCHEMA.MY_VIEW
# CORTEX_SEMANTIC_MODEL=@MY_DB.MY_SCHEMA.MY_STAGE/model.yaml

# optional:
# CORTEX_MODEL=claude-3-5-sonnet             # the Cortex COMPLETE model the worker/judge use
```

> The PAT is passed in place of a password — Snowflake accepts a Programmatic Access Token
> anywhere a password is accepted. Scope the PAT to `SNOWFLAKE_ROLE`, and make sure that role
> holds `SNOWFLAKE.CORTEX_USER` + SELECT on your tables.

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

### Ask your own question, or chat interactively

```powershell
py -3 run_local.py inventory_balance "Which 5 items have the most stock?"   # one-shot: your question
py -3 chat_local.py inventory_balance                                        # interactive: ask in a loop
py -3 run_evals.py dq_qals                                                   # offline golden-set gate
```

**`chat_local.py` — interactive REPL.** Build the graph once, then keep asking:

```powershell
py -3 chat_local.py                                # default pack (dq_qals, or $USE_CASE)
py -3 chat_local.py inventory_balance              # a specific pack
$env:USE_CASE="goa_spend"; py -3 chat_local.py     # pick the pack via env instead of an arg
```

Type questions at the `you>` prompt; leave with a blank line, `exit`, `quit`, or Ctrl-C / Ctrl-D.
Provider / model / SQL tool come from `.env` (same as `run_local.py`); the graph is built ONCE and
reused, so a Cortex session opens once, not per question. Each question is an INDEPENDENT run (no
multi-turn memory). Like `run_local.py` / `run_evals.py`, it's a **runner script** — it never modifies
`engine/` or source.

### What breaks (and the fix)

| Symptom | Cause → fix |
|---|---|
| `No Snowflake token…` | `SQL_TOOL=cortex` but no `SNOWFLAKE_PAT` (+`SNOWFLAKE_HOST`). Add them. |
| `KeyError: 'SNOWFLAKE_PAT'` (or account/user) | a required value is missing from `.env` — `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_USER`, `SNOWFLAKE_PAT`. |
| session auth error (invalid password/token) | the PAT is wrong/expired or not scoped to `SNOWFLAKE_ROLE`. Regenerate it. |
| `…pack declares no semantic layer` (or `needs a snowflake: semantic layer`) | the pack has no `usecases/<pack>/semantic_layer.yaml` (or no `snowflake:` block). Add one, or set the `CORTEX_SEMANTIC_VIEW` / `CORTEX_SEMANTIC_MODEL` test override in `.env`. |
| HTTP **401/403** on the Analyst call | PAT invalid/expired, or the role lacks Cortex. Regenerate the PAT; confirm `SNOWFLAKE.CORTEX_USER`. |
| HTTP **400** naming the semantic model/view | the view name/stage path is wrong or the role can't see it. Re-check `SHOW SEMANTIC VIEWS;`. |
| `Unknown user-defined function SNOWFLAKE.CORTEX.COMPLETE` | your session role can't see Cortex. Grant `SNOWFLAKE.CORTEX_USER`, or set `SNOWFLAKE_SECONDARY_ROLES=all` so the session uses whichever granted role has it. |
| `Incoming request with IP ... is not allowed` | a Snowflake **network policy** blocks this IP; run from an allowlisted network/VPN or have an admin allow your egress IP. |
| SQL run error (table not found / no access) | the role can't SELECT the tables behind the view. Grant SELECT. |

> Prefer key-pair auth over a PAT? That's a small change in `snowpark_session()`
> (`engine/llm_client.py`) plus a key-pair for the REST call — ask if you want it wired.

---

## 4. Databricks locally — honest status

The Databricks worker (`DatabricksClient`, or `provider: litellm` + `databricks/<endpoint>`) and the
Genie SQL tool (`GenieTool`) are **wired**. The worker path is live-verified (the `generic_joke` /
`generic_math` packs); the Genie path is unit-tested against a fake `databricks-sdk` client and still
needs its first live run. So:

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

### 4b. Hitting **real** Databricks + Genie

1. The pack declares its Genie space (functional config lives in the pack, never in env):
   ```yaml
   # usecases/<pack>/semantic_layer.yaml
   databricks:
     genie_space: 01ef1234abcd5678          # REQUIRED for SQL_TOOL=genie
     metric_view: main.gold.my_metric_view  # OPTIONAL: enables term->value binding (needs a warehouse)
   ```
2. `.env` carries only credentials + selection — the same pattern as Snowflake:
   ```dotenv
   WORKER_PROVIDER=databricks
   WORKER_MODEL=<serving-endpoint>         # databricks has NO default model (or use litellm + databricks/<endpoint>)
   SQL_TOOL=genie
   DATABRICKS_HOST=https://<your-workspace-host>
   DATABRICKS_TOKEN=dapi...
   # DATABRICKS_WAREHOUSE_ID=<sql warehouse id>   # optional: metric-view value binding
   # GENIE_TIMEOUT_SECONDS=120                    # optional: Genie plans + runs the SQL
   ```
   ```powershell
   py -3 run_local.py <pack>
   ```
   `pip install databricks-sdk` for the Genie path. The token needs CAN RUN on the space and SELECT on
   its tables (Genie runs the SQL as the caller). What you should see in the log: the Genie SQL captured
   as `last_sql`, rows as `col | col` text, and — for an empty result — the normal reformulate/escalate
   path, identical to Cortex.

Tip: test **one half at a time** — `WORKER_PROVIDER=databricks` with `SQL_TOOL=mock` first (isolates
the LLM), then flip `SQL_TOOL=genie`. Same isolation logic as §3.

> Semantic layer note: on Databricks the equivalent of a Semantic View is a **Metric View**,
> and Genie is pointed at it via the Genie space. It does not share a format with Snowflake's;
> that's expected (decision #2). `metric_view:` in the pack is the space's declared source, used
> for value lookups — it never bypasses Genie.

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
