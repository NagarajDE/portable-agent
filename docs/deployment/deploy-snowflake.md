# Deploy to Snowflake (SPCS) — step by step for a first-timer

This guide gets the agent running as a **container** inside Snowflake, using **Snowpark
Container Services (SPCS)**. No prior container experience assumed.

> **We deploy on MOCK first.** The first deploy runs the built-in MOCK provider — it needs
> **no credentials and no semantic model**, so it's guaranteed to work end-to-end and proves
> your container/service plumbing. The Cortex adapters are already wired; section 10 shows
> the config to switch to real Cortex. Do the mock deploy first; don't skip ahead.

---

## 0. The 60-second mental model

| Term | What it is (plain) |
|---|---|
| **Image** | A zip of our app + Python, built by Docker. Runs identically anywhere. |
| **Image repository** | A private registry *inside Snowflake* where your image is stored. |
| **Compute pool** | The VMs Snowflake gives you to run containers on. |
| **Service** | Your running container(s) + the rule for how to reach them (an HTTP endpoint). |
| **Spec (`spec.yaml`)** | The recipe that tells the service which image to run, what env vars to set, and which port to expose. |

Flow: **build image → push to Snowflake's repo → make a compute pool → create a service from the spec → call its endpoint.**

---

## 1. Install the tools (one time)

1. **Docker Desktop** — https://www.docker.com/products/docker-desktop . Install, launch it,
   and confirm:
   ```bash
   docker --version
   ```
2. **Snowflake CLI** (`snow`) — the modern CLI (not the old `snowsql`):
   ```bash
   pip install snowflake-cli-labs
   snow --version
   ```
3. **Connect the CLI to your account** (creates a saved connection named `default`):
   ```bash
   snow connection add
   ```
   It will prompt for account, user, password/authenticator, role, warehouse. Test it:
   ```bash
   snow connection test
   ```

You also need a **role** that can create compute pools, image repositories, and services
(commonly `ACCOUNTADMIN`, or a role your admin granted `CREATE COMPUTE POOL` etc.), and a
**database + schema** to work in. This guide uses `MY_DB.MY_SCHEMA` — replace with yours.

---

## 2. Create a database/schema and an image repository (in Snowflake)

Run these in a Snowflake worksheet (or `snow sql -q "..."`). Replace names as you like:

```sql
CREATE DATABASE IF NOT EXISTS MY_DB;
CREATE SCHEMA   IF NOT EXISTS MY_DB.MY_SCHEMA;
CREATE IMAGE REPOSITORY IF NOT EXISTS MY_DB.MY_SCHEMA.AGENT_REPO;

-- Get the repository URL you will push to:
SHOW IMAGE REPOSITORIES IN SCHEMA MY_DB.MY_SCHEMA;
```

Copy the **`repository_url`** from the result. It looks like:
```
<orgname>-<acctname>.registry.snowflakecomputing.com/my_db/my_schema/agent_repo
```

---

## 3. Build the image and push it

Do all of this from the **repo root** (the folder that contains `engine/`, `shared/`,
`usecases/`).

```bash
# 3a. Log Docker in to Snowflake's registry (uses your snow connection):
snow spcs image-registry login

# 3b. Build the image. --platform linux/amd64 is REQUIRED if you're on an Apple-Silicon Mac
#     (SPCS runs amd64). It's harmless on Windows/Intel.
docker build --platform linux/amd64 -f engine/platform_snowflake/Dockerfile -t dq_agent:latest .

# 3c. Tag it with your repository URL from step 2 (note the /dq_agent:latest suffix):
docker tag dq_agent:latest <repository_url>/dq_agent:latest

# 3d. Push:
docker push <repository_url>/dq_agent:latest
```

> If `docker build` fails, read the last lines — it's almost always a typo in the path or
> Docker Desktop not running. If `push` says "unauthorized", re-run step 3a.

---

## 4. Point the spec at your image

Open [`engine/platform_snowflake/spec.yaml`](../../engine/platform_snowflake/spec.yaml) and
set the `image:` line to your **path** (lowercase, no hostname needed):

```yaml
      image: "/my_db/my_schema/agent_repo/dq_agent:latest"
```

Leave the env block as-is for now — it's set to **MOCK**, so the first deploy just works.

---

## 5. Create a compute pool

```sql
CREATE COMPUTE POOL IF NOT EXISTS DQ_POOL
  MIN_NODES = 1
  MAX_NODES = 1
  INSTANCE_FAMILY = CPU_X64_XS
  AUTO_RESUME = TRUE
  AUTO_SUSPEND_SECS = 300;

SHOW COMPUTE POOLS;   -- wait until STATE = IDLE or ACTIVE
```

`CPU_X64_XS` is the smallest/cheapest. `AUTO_SUSPEND_SECS = 300` parks it after 5 idle
minutes so you're not billed while nothing runs.

---

## 6. Create the service from the spec

Using the Snowflake CLI (points at your local `spec.yaml`):

```bash
snow spcs service create dq_agent \
  --compute-pool DQ_POOL \
  --spec-path engine/platform_snowflake/spec.yaml \
  --database MY_DB --schema MY_SCHEMA
```

(Equivalent in SQL if you prefer: `CREATE SERVICE MY_DB.MY_SCHEMA.DQ_AGENT IN COMPUTE POOL DQ_POOL FROM SPECIFICATION $$ <paste spec.yaml> $$;`)

---

## 7. Watch it come up

```sql
-- Overall status (want status = READY):
SELECT SYSTEM$GET_SERVICE_STATUS('MY_DB.MY_SCHEMA.DQ_AGENT');

-- Container logs (our JSON trace events + any errors). '0' = instance, 'dq-agent' = container name:
SELECT SYSTEM$GET_SERVICE_LOGS('MY_DB.MY_SCHEMA.DQ_AGENT', 0, 'dq-agent', 200);
```

First start pulls the image and can take a couple of minutes. If status stays `PENDING`,
read the logs — a bad `image:` path in the spec is the usual cause.

---

## 8. Get the URL and call it

```sql
SHOW ENDPOINTS IN SERVICE MY_DB.MY_SCHEMA.DQ_AGENT;
```

Copy the **`ingress_url`**. A `public: true` endpoint is protected by **Snowflake login**:

- **Easiest check:** open `https://<ingress_url>/healthz` in a browser. It'll ask you to log
  in with Snowflake, then show `{"status":"ok","use_case":"dq_qals","tracer":"stdout"}`.
- **Call `/invoke`:** it's a `POST` with a JSON body `{"question": "..."}`. Because the
  endpoint requires Snowflake auth, programmatic calls need a Snowflake OAuth token in an
  `Authorization: Bearer <token>` header. For a first test, the browser `/healthz` check is
  enough to prove the service is live.

A successful `/invoke` returns:
```json
{"answer": "...", "score": 18, "run_id": "abc123..."}
```
The `run_id` is your correlation key — search the service logs for it to see the full
generate→evaluate→refine trace for that request.

---

## 9. Everyday commands

```sql
ALTER SERVICE MY_DB.MY_SCHEMA.DQ_AGENT SUSPEND;   -- stop (stops billing for the container)
ALTER SERVICE MY_DB.MY_SCHEMA.DQ_AGENT RESUME;    -- start again
DROP  SERVICE MY_DB.MY_SCHEMA.DQ_AGENT;           -- remove it
ALTER COMPUTE POOL DQ_POOL SUSPEND;               -- park the VMs
```

To ship a code change: rebuild (step 3b), push (3d), then
`ALTER SERVICE ... FROM SPECIFICATION ...` or drop+recreate the service.

---

## 10. Switch from MOCK to real Cortex

The Cortex adapters are **already wired** — no code to write. Inside SPCS they authenticate
with the OAuth token Snowflake injects at `/snowflake/session/token` (no secrets to store).
To make the service answer from real data:

1. **Build a Cortex Analyst semantic model** — a YAML describing your tables/metrics —
   and upload it to a stage. Note its path, e.g. `@MY_DB.MY_SCHEMA.MODELS/dq.yaml`.
2. **Flip the spec** — in [`spec.yaml`](../../engine/platform_snowflake/spec.yaml), comment
   the three MOCK lines and uncomment the Cortex block:
   ```yaml
        WORKER_PROVIDER: "cortex"
        SQL_TOOL: "cortex"
        CORTEX_SEMANTIC_MODEL: "@MY_DB.MY_SCHEMA.MODELS/dq.yaml"
        SNOWFLAKE_WAREHOUSE: "MY_WH"     # runs COMPLETE + the Analyst-generated SQL
        SNOWFLAKE_DATABASE: "MY_DB"
        SNOWFLAKE_SCHEMA: "MY_SCHEMA"
   ```
3. **Grant the service's role** access to Cortex + the data: `SNOWFLAKE.CORTEX_USER`
   database role, USAGE on the warehouse, and **SELECT only** on the tables the semantic
   model uses. Do **not** grant INSERT/UPDATE/DELETE — Cortex only needs to read, and a
   read-only role means a mis-generated query physically cannot write. (The app also enforces
   SELECT-only + a statement timeout as a backstop, but the grant is the real control.)
4. **Recreate the service** (`DROP SERVICE` then `snow spcs service create ...` again — no
   rebuild needed unless you changed code).

**Recommended: test the real Cortex path on your laptop first** — no container, no deploy.
See **[Appendix A](#appendix-a--test-the-real-cortex-path-locally-no-container)** below.

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `docker build` can't find files | Run it from the **repo root**, with `-f engine/platform_snowflake/Dockerfile`. |
| `docker push` → unauthorized | Re-run `snow spcs image-registry login`. |
| Service stuck `PENDING` | Wrong `image:` path in `spec.yaml`, or compute pool not `ACTIVE/IDLE`. Check `SYSTEM$GET_SERVICE_STATUS`. |
| Service `READY` but `/invoke` errors on `cortex` | Missing `CORTEX_SEMANTIC_MODEL`/warehouse, or the service role lacks Cortex/data grants (step 10.3). Check `SYSTEM$GET_SERVICE_LOGS`; go back to mock to isolate. |
| `/healthz` asks me to log in | Expected for a `public` endpoint — that's Snowflake auth. Log in. |
| Image runs on my laptop but not SPCS | You built ARM; rebuild with `--platform linux/amd64`. |

---

## Appendix A — Test the real Cortex path locally (no container)

You do **not** need to deploy to test that Cortex works. Run the loop on your laptop
pointing at your Snowflake account. Same code as in the container — only the auth differs
(locally you use your login + a token; in SPCS Snowflake injects one).

### A1. What you need from Snowflake
- Account with **Cortex enabled** and a role granted the `SNOWFLAKE.CORTEX_USER` database role.
- A **warehouse** you can use, and **SELECT** on the tables your questions touch.
- For the **Cortex Analyst** step only: a **semantic layer** — an existing **Semantic View**
  (`CORTEX_SEMANTIC_VIEW`) *or* a **semantic model YAML** on a stage (`CORTEX_SEMANTIC_MODEL`) —
  and a **Programmatic Access Token (PAT)** for the REST call. (Generate a PAT in Snowsight:
  *your user → Settings → Authentication → Programmatic access tokens*, scoped to your role.)
  Windows-first walkthrough incl. the Semantic View path: [`../run-locally-windows.md`](../run-locally-windows.md).

### A2. Put your credentials in a `.env` file (this is "where the creds go")
From the **repo root**:
```bash
cp .env.example .env          # PowerShell:  Copy-Item .env.example .env
pip install python-dotenv     # lets run_local.py auto-read the .env
```
Open `.env` and set (leave the rest as-is). **This file is git-ignored — it is never committed.**
```dotenv
WORKER_PROVIDER=cortex
SQL_TOOL=cortex

SNOWFLAKE_ACCOUNT=ab12345.us-east-1          # your account identifier
SNOWFLAKE_USER=YOUR_USER
SNOWFLAKE_PASSWORD=YOUR_PASSWORD
SNOWFLAKE_ROLE=YOUR_ROLE                      # must have SNOWFLAKE.CORTEX_USER
SNOWFLAKE_WAREHOUSE=YOUR_WH                   # runs COMPLETE + the Analyst-generated SQL
SNOWFLAKE_DATABASE=YOUR_DB
SNOWFLAKE_SCHEMA=YOUR_SCHEMA

# Cortex Analyst (SQL_TOOL=cortex) also needs:
SNOWFLAKE_HOST=ab12345.us-east-1.snowflakecomputing.com   # account host for the REST call
SNOWFLAKE_PAT=<your programmatic access token>
CORTEX_SEMANTIC_MODEL=@YOUR_DB.YOUR_SCHEMA.YOUR_STAGE/model.yaml

# optional:
# CORTEX_MODEL=claude-3-5-sonnet
```

### A3. Run it — in two steps, so problems are easy to pin down

**Step 1 — test Cortex COMPLETE only** (the LLM half; needs no PAT or semantic model). In
`.env` temporarily set `SQL_TOOL=mock`, then:
```bash
python run_local.py dq_qals
```
If you get an answer with a climbing score, Cortex `COMPLETE` + your credentials work.

**Step 2 — add Cortex Analyst** (the SQL half). Set `SQL_TOOL=cortex` in `.env`, then:
```bash
python run_local.py dq_qals
```
Now the loop calls Cortex Analyst (REST) to turn the question into SQL, runs that SQL on your
warehouse, and answers from the real rows.

### A4. What you should see / what breaks
- **Works:** normal loop output ending in `BEST SCORE : n/18` with a real answer.
- **`No Snowflake token...`** → you set `SQL_TOOL=cortex` but no `SNOWFLAKE_PAT` (+`SNOWFLAKE_HOST`). Add them, or use `SQL_TOOL=mock` for Step 1.
- **`KeyError: 'SNOWFLAKE_PASSWORD'`** (or account/user/…) → a required `SNOWFLAKE_*` value is missing from `.env`.
- **`... CORTEX_SEMANTIC_MODEL`** → set the stage path in `.env`.
- **HTTP 401/403 on the Analyst call** → PAT invalid/expired or the role lacks Cortex; regenerate the PAT and confirm `SNOWFLAKE.CORTEX_USER`.
- **SQL run error** → the role can't see the tables in the semantic model; grant SELECT.

> Prefer not to use a password locally? You can still validate everything with the built-in
> mock (`WORKER_PROVIDER=mock SQL_TOOL=mock`) — but that doesn't touch Snowflake. Appendix A
> is specifically for confirming the **real** Cortex calls before you containerize.

---

## Appendix B — Enable memory (episodic + feedback flywheel)

Optional and **off by default** (`MEMORY_STORE=none`). Turn it on to persist every run and
any user feedback into two append-only tables **you own** — the raw material you later curate
into `usecases/*/exemplars` + `evals`. (This is *not* a chat-history/thread feature — that's
deliberately parked; see `PROJECT_CONTEXT.md` §12.9.)

### B1. Turn it on
- **In the service:** in `spec.yaml` set `MEMORY_STORE: "snowflake"` (uncomment the line).
- **Locally:** in `.env` set `MEMORY_STORE=sqlite` (writes a local file — no Snowflake needed),
  or `MEMORY_STORE=snowflake` with your `SNOWFLAKE_*` creds.

### B2. Grants (one time)
The service's role needs to create + write the tables in its current database/schema:
```sql
GRANT CREATE TABLE ON SCHEMA MY_DB.MY_SCHEMA TO ROLE <service_role>;
-- after first run the tables exist; INSERT is implicit for the owner role.
```
On first use the app auto-creates `PORTABLE_AGENT_INTERACTIONS` and `PORTABLE_AGENT_FEEDBACK`.

### B3. Send feedback
`POST /feedback` with the `run_id` you got back from `/invoke`:
```json
{"run_id": "abc123...", "rating": "down", "note": "missed the root cause"}
```
(Returns `{"status": "memory_disabled"}` if `MEMORY_STORE=none`.)

### B4. Use it (the flywheel)
Find weak runs to curate into verified exemplars:
```sql
-- low-scoring or thumbs-down answers, newest first
SELECT i.run_id, i.question, i.answer, i.score, f.rating, f.note
FROM PORTABLE_AGENT_INTERACTIONS i
LEFT JOIN PORTABLE_AGENT_FEEDBACK f USING (run_id)
WHERE i.score < 16 OR f.rating = 'down'
ORDER BY i.ts DESC;
```
Turn the good Q→answer patterns into entries in `usecases/<pack>/exemplars/` and the failure
cases into `usecases/<pack>/evals/golden_set.yaml`. That promotion is the portable win — it
moves tuning from a vendor surface into git.

### B5. Delete on request (PII)
Rows are `run_id`-keyed and append-only, so honoring a deletion request is one statement:
```sql
DELETE FROM PORTABLE_AGENT_INTERACTIONS WHERE run_id = '<id>';
DELETE FROM PORTABLE_AGENT_FEEDBACK      WHERE run_id = '<id>';
```
Remember: once you store question/answer text, retention + PII are **your** responsibility.
