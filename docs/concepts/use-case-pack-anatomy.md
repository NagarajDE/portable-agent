# Use-case pack anatomy — what each folder is, and how to add a new agent

One agent = one folder under `usecases/<pack>/`. This is the guide for authoring or extending a pack:
what each file is for, how the pack composes with the shared tier, and a checklist for a new pack.

> Prereqs: [`the-refine-loop.md`](the-refine-loop.md) (the loop the prompts drive) and
> [`end-to-end-flow.md`](end-to-end-flow.md) (which artifact runs when).

---

## 1. The standard skeleton

```
usecases/<pack>/
  __init__.py            REQUIRED  — makes the pack importable (so fixtures.py can be loaded)
  config.yaml            REQUIRED  — inherits: [shared], name, sample_task; optional tools:, limits
  semantic_layer.yaml    AI+BI only — the native semantic view/model this pack queries (section 5)
  prompts/
    generate.md          REQUIRED  — the persona / worker instructions (the one truly per-agent prompt)
    rubric.md            optional  — ONLY to override the shared judge rubric for this domain
    refine.md            optional  — ONLY to override the shared refine (rewrite) instructions
  skills/*.md            optional  — domain how-to; concatenated AFTER the shared skills
  exemplars/*.yaml       optional  — few-shot examples (verified Q->SQL or input->output)
  evals/golden_set.yaml  recommended — regression questions (test-time only, run by run_evals.py)
  fixtures.py            REQUIRED for mock runs — canned rows so the pack runs with no creds
```

Copy `usecases/_TEMPLATE/` to start — it's a runnable minimal pack. See section 6.

### What each file is for
- **config.yaml** — the knobs: `inherits: [shared]`, a human `name`, a `sample_task`, **`loop: true`**
  (below), and optionally `tools:`, scoring (`max_score`, `pass_score`/`threshold`), `max_iters`,
  `eval_retries`, `max_stall`, `max_data_retries`, `zero_is_no_data`, `exclude_shared_skills:`
  (section 4), and `frame_query` / `frame_skills` (below).
  - **`max_output_tokens`** (optional, 256..200000) — the output-token ceiling for this pack's `generate`
    and `refine` calls, when its answers are long (a multi-step synthesis listing many positions). Unset →
    the deployment default `LLM_MAX_TOKENS` (8192). A ceiling, not a spend.
  - **`loop`** (default **off**) — `loop: true` turns on the evaluate→refine loop (scored answers). Without
    it the run is `generate → END`: retrieval / multi-step gathering, framing, reformulation and the no-data
    escalation all still run, but the worker's answer is final and **unscored** (`score: None`,
    `status: ok`). `loop: true` with no buildable evaluator warns and runs non-loop. Every shipped pack
    sets it; see [the-refine-loop.md §0](the-refine-loop.md).
  The two blank-data
  knobs:
  - **`max_data_retries`** (default `1`, max `5`, `0` = off) — how many times a **blank** retrieval may be
    rephrased and re-tried before the run escalates unscored. See
    [retries-explained.md](retries-explained.md).
  - **`zero_is_no_data`** (default `false`) — also treat a **single all-zero/NULL row** (`COUNT(*) = 0`,
    `SUM(...) = NULL`) as blank. Opt-in per pack because it is indistinguishable from a genuine zero:
    turn it on where `0` always means "a filter or status label matched nothing" (all four AI+BI packs
    set it), leave it off where `0` is a legitimate answer.
  - **`frame_query`** (default `false`) — before the **first** retrieval, let the worker rewrite the
    question into a precise text-to-SQL request using the pack framing `skills` (*skill-informed query
    framing*), so a named value is bound to the column that actually holds it (e.g. "the IT Software
    category" → `ExecutiveCategory = 'IT Software'`). The rewrite is **minimal** — it binds a named value
    to its column and names the default measure, and must NOT add a filter, date grain, scope, or derived
    metric the user didn't ask for. Fail-safe: any drift, empty reply, or error falls back to the raw
    question, so it can only *add* precision. It applies to whatever path the question drives — SQL, the
    agentic tool loop, or deterministic tools — but not a toolless pack. All four AI+BI packs enable it;
    the example packs enable it too (low value without a semantic view, but harmless). See section 5 and
    [end-to-end-flow.md](end-to-end-flow.md).
  - **`frame_skills`** (optional list; default = ALL skills) — the skill files framing uses, so a pack that
    ships many answer-side skills can keep the framing prompt focused on just its value→column map (e.g.
    `frame_skills: [dimension_map.md]`). Answer synthesis (`generate.md`) and `refine.md` always use the
    FULL skill set. Unset means framing sees all skills (unchanged for a pack with a small skill set).
  - **`recover_value_dims`** (optional list of `TABLE.DIM` paths) — *reactive value recovery (a safety net).*
    When a query comes back **empty**, the engine looks up the **real distinct values** of these columns from
    the live view and hands them to the reword step, so it re-asks with the **exact stored value** — "active"
    → the real `BUSINESS_STATUS` values, "instruments" → the stored singular `Instrument` — instead of a
    literal that matches nothing. It fires **only on a blank result** (questions that return rows the first
    time pay nothing), once per session (cached), and is a **no-op** unless the SQL tool exposes
    `distinct_values()` (Cortex only, so **MOCK stays deterministic**) and fail-safe (any error → the retry
    just rewords as before). Presence of a non-empty list = enabled. Enabled on `goa_spend`,
    `inventory_balance`, `procurement_contracts`. The optional note (`frame_skills`) only *hints which column*
    a value lives in for the first try; the **live values are the source of truth** for what's actually stored.
- **semantic_layer.yaml** — *Analyst/Genie packs only.* Names the native semantic layer this pack
  queries (Snowflake view / stage-YAML, or Databricks metric-view / Genie-space). Its **presence marks
  the pack as AI+BI-backed**; absent means the engine never looks for one. Read from the pack, never
  from env (section 5).
- **prompts/generate.md** — the worker's instructions: persona + how to answer, with `{placeholders}`
  the engine fills (`{task}`, `{data}`/`{observations}`, `{skills}`, `{exemplars}`, `{tools}`,
  `{revision}`). Always pack-owned — there is no shared default for `generate.md`.
- **prompts/rubric.md** — the judge's grading instructions. Ship one ONLY if this domain needs
  different scoring than the shared rubric; otherwise the pack inherits `shared/prompts/rubric.md`.
- **prompts/refine.md** — the worker's rewrite instructions. Almost never overridden; the shared one
  works for every pack.
- **skills/*.md** — durable domain rules and house style. Concatenated onto the shared skills.
- **exemplars/*.yaml** — few-shot demonstrations (section 3).
- **evals/golden_set.yaml** — held-out questions + `expect_contains` substrings; a regression gate,
  not training data. Test-time only.
- **fixtures.py** — a `MockSQLTool` (and optional canned answers) so the pack runs on the mock provider
  with no credentials. Ignored once `SQL_TOOL`/`WORKER_PROVIDER` are real.

---

## 2. Composition: inherit, override, concatenate (the one reference)

How a pack combines with `shared/`, and exactly what the mechanism is:

| artifact | shared default? | rule | how it's triggered | merge semantics |
|---|---|---|---|---|
| `config.yaml` | yes | **INHERIT + MERGE** (pack wins) | `inherits: [shared]` in the pack config | maps shallow-merged; scalars and lists **replaced** (not deep-merged) |
| `prompts/generate.md` | no (pack-only) | pack-owned | file present in the pack | n/a |
| `prompts/rubric.md` | yes | **OVERRIDE** (pack ELSE shared) | **file presence** — no config entry | whole-file replacement (never concatenated) |
| `prompts/refine.md` | yes | **OVERRIDE** (pack ELSE shared) | **file presence** — no config entry | whole-file replacement |
| `skills/*.md` | yes | **CONCATENATE** (shared + pack) | both dirs globbed, sorted | shared first, then pack; opt out via `exclude_shared_skills:` |
| `exemplars/*.yaml` | no | pack-only | all `*.yaml` in `pack/exemplars` globbed | items appended in filename order |
| `evals/golden_set.yaml` | no | pack-only | `run_evals.py` reads it | n/a (test-time) |
| `fixtures.py` | no | pack-only | Python import `usecases.<pack>.fixtures` | n/a |
| `semantic_layer.yaml` | no | pack-only (**presence = AI+BI pack**) | **file presence** — read by the engine | read as-is; NOT inherited/merged; env is only a test-time override |

**Override is by file presence, NOT config.** To override the shared rubric or refine for one pack,
just drop `usecases/<pack>/prompts/rubric.md` (or `refine.md`) into the pack — you do **not** list it
anywhere. `config.yaml`'s `inherits:` controls **only** config merging, nothing else. (Implemented in
`_prompt()` / `load_config()` in [`engine/graph.py`](../../engine/graph.py).)

Example: `dq_qals` and `inventory_balance` ship their own `rubric.md` (override); the other packs have
no `rubric.md`, so they inherit the shared one.

---

## 3. Exemplars — formats and the `kind:` discriminator

Exemplars are curated few-shot pairs injected into `generate.md`. Two formats are supported:

```yaml
# text_to_sql — a verified question -> SQL pair
- kind: text_to_sql        # optional; if omitted, the presence of `sql` selects this format
  question: "..."          # required
  sql: "SELECT ..."        # required

# input_output — a domain-neutral demonstration (non-SQL packs)
- kind: input_output       # optional; the default when there is no `sql` key
  input: "..."             # required (alias: question)
  output: "..."            # required (alias: answer)
```

- Prefer an **explicit `kind:`** once a pack mixes formats — it's a real discriminator, not a guess.
  When `kind:` is absent, the engine falls back to key-sniffing (presence of `sql`) for backward
  compatibility. (See `_format_exemplar()` in [`engine/graph.py`](../../engine/graph.py).)
- Filename is not the dispatch: `verified_queries.yaml` vs `examples.yaml` is just a naming convention
  (all `*.yaml` are globbed). Use `verified_queries.yaml` for genuinely schema-validated SQL; use
  `examples.yaml` (or any name) otherwise.

---

## 4. Skills — universal by default, opt out when they don't apply

`load_skills()` concatenates `shared/skills/*.md` then `pack/skills/*.md`. The shared skills are meant
to be universal:
- `reporting.md` — house reporting style (applies to every agent).
- `sql_safety.md` — SELECT-only safety (applies **only to SQL agents**).

A non-SQL pack should NOT get `sql_safety` injected (wasted context at best, conflicting instructions
at worst). Opt out per pack in `config.yaml`:

```yaml
exclude_shared_skills: [sql_safety]   # drop a shared skill by filename stem
```

Pack skills are never excluded (they're all in-scope by construction). Default (no key) = include all
shared skills, so SQL packs are unchanged. `api_assistant` (the non-SQL example) uses this.

---

## 5. The boundary: skills guide the worker/judge, not the executed query

Important, because it's easy to get wrong. The evidence step (tools / SQL) runs **before** `generate`,
and with Cortex Analyst the SQL is generated from the **semantic model**, not from your skills. So a
skill phrased as a query rule ("filter MostRecentSnapshot = TRUE") cannot change the query that already
ran.

- Put **query semantics** (default filters, joins, metric SQL) in the **semantic model** (Snowflake
  Semantic View / Databricks Metric View). By design that layer is native and rebuilt per platform
  (it does not port) — but the pack records *which* layer it binds to in `semantic_layer.yaml` (below).
- Put **interpretation and presentation** rules in `skills/` — things the worker and judge control:
  "if the evidence spans multiple snapshots, don't sum across them; report per snapshot", "show a
  material description only when the evidence carries one", "lead with the quantified finding".

`inventory_balance/skills/` is written this way (interpret-the-evidence, not shape-the-query).

### Opt-in exception: skill-informed query FRAMING (`frame_query`)

There is one sanctioned way for skills to reach the query — and it shapes the **question**, not the SQL.
With `frame_query: true` (section 1), the worker rewrites the user's question into a precise text-to-SQL
request using the pack `skills` **before** the first retrieval; Cortex Analyst then generates the SQL from
the semantic model exactly as usual. So skills can *disambiguate* ("the IT Software category" → the
`ExecutiveCategory` dimension that actually holds that value) without ever writing SQL. Query **semantics**
(default filters, joins, metric SQL) still belong in the semantic model — framing only makes the question
name the right column/measure so Analyst doesn't guess a wrong one and return zero rows. It is fail-safe
(a drifting or empty rewrite falls back to the raw question), so enabling it can only *add* precision.
Each AI+BI pack ships a small value→column map used ONLY for framing (via `frame_skills:`) — e.g.
`goa_spend/skills/dimension_map.md` (which-column-does-this-value-live-in) — while its larger answer-side
skills stay out of the framing prompt. All four AI+BI packs enable framing today.

**Reactive value recovery (`recover_value_dims`).** The optional note is only a *first-try hint*: it says
*which column* a concept lives in, and any values it mentions are FYI examples, not authoritative. The real
translation happens as a **safety net**: when a query comes back **empty**, the engine looks up the
column's **real distinct values** from the live view and the reword step re-asks with the **exact stored
value** — so it never depends on a literal like `'active'` or a mis-cased `Instrument`, and a stale note
does no harm. It fires **only on a blank result** (no cost when the first try works), is Cortex-only (no-op
on MOCK), fail-safe, and cached per session; see section 1 for the knob. This is why the notes no longer
enumerate values — the live data is the source of truth, the note just points to the column.

### Declaring the semantic layer (Analyst/Genie packs only)

An AI+BI pack names its native semantic object in `usecases/<pack>/semantic_layer.yaml`. The engine
reads it from the pack and passes it to the live text-to-SQL tool:

```yaml
snowflake:
  view: DB.SCHEMA.MY_SEMANTIC_VIEW              # a native Semantic View
  # model_file: "@DB.SCHEMA.STAGE/model.yaml"   # OR a semantic-model YAML on a stage (view wins if both)
databricks:                                     # SQL_TOOL=genie (Databricks Genie, wired)
  genie_space: 01ef1234abcd5678                 # REQUIRED -- Genie is addressed by space id
  metric_view: main.schema.my_metric_view       # OPTIONAL -- the space's governed source; with
                                                #   DATABRICKS_WAREHOUSE_ID set it also enables
                                                #   term->value binding (DESCRIBE + distinct values)
```

- **Presence = capability.** A file present marks the pack AI+BI-backed; the active tool picks the
  block (`SQL_TOOL=cortex` -> `snowflake`, `SQL_TOOL=genie` -> `databricks`). A pack with **no file**
  never triggers a lookup — the guard rail for non-AI+BI packs.
- **Guard rail.** Selecting a live tool (`cortex`/`genie`) for a pack that declares no matching block
  is a clear error, not a silent misfire.
- **Engine reads the pack, NOT env.** `CORTEX_SEMANTIC_VIEW` / `CORTEX_SEMANTIC_MODEL` are a
  **test-only override** applied by the runner scripts (`run_local.py` / `run_evals.py`), never by
  `engine/`. (See `load_semantic_layer()` and `get_sql_tool()` in
  [`engine/graph.py`](../../engine/graph.py) / [`engine/sql_tool.py`](../../engine/sql_tool.py).)

**Config vs. credentials — two categories.** The semantic layer is *functional config* → it lives in
the pack. The *credentials/identity* to reach it (the SPCS-injected OAuth token, or a local
`SNOWFLAKE_PAT` + account/warehouse) come from the runtime environment / platform injection — never
committed, never in a pack file.

---

## 5b. Per-pack provider/model, the business glossary, and term→value binding

### Provider/model defaults (`models:`)
A pack may declare which provider/model its worker and judge use. Env always wins (that's the
one-env-var migration flip), so leave `WORKER_*`/`EVAL_*` unset for the pack's block to apply:
```yaml
models:
  worker:    { provider: cortex, model: "claude-opus-5" }
  evaluator: { provider: cortex, model: "claude-opus-4-8" }   # judge with a DIFFERENT model (loop: true)
  # planner: { provider: cortex, model: "claude-haiku-4-5" }  # OPTIONAL, tool_mode: planned only
```
Providers: `mock | cortex | databricks | litellm` (case-insensitive). `databricks` needs an explicit
serving-endpoint model (no default); `litellm` needs a `provider/model` string. Precedence per role:
env `WORKER_MODEL`/`EVAL_MODEL`/`PLANNER_MODEL` > pack `models:` > the provider's built-in default. The
evaluator is only used when the pack sets `loop: true`; the planner is only used by planned packs (unset
= the worker plans). The AI+BI packs ship Cortex defaults; `WORKER_PROVIDER=mock SQL_TOOL=mock` runs any
of them on fixtures.

### Business glossary (`glossary.yaml`)
What a business term MEANS and HOW IT MAPS to the data. Two scopes, merged **by term, pack wins**:
`shared/glossary.yaml` (only terms every pack defines identically) + `usecases/<pack>/glossary.yaml`.
```yaml
- term: "active contract"
  aliases: ["live contract", "in-force"]
  meaning: "A signed contract currently in force."
  maps_to: "BUSINESS_STATUS IN ('Executed', 'Approved')"
```
It reaches **both** the query side (framing / planning) and the answer side (generate / refine) by
riding on the skills text — no prompt changes. Absent → nothing injected.

### Term→value binding — hybrid, never hand-author every column
The class of bug: "active" isn't a stored value (`Executed`/`Approved` are) → 0 rows → `no_data`. A
100–200-column view can't be hand-enumerated, so binding is layered, cheapest-first:
1. **Hand-declared few** — `frame_skills` / `dimension_map.md` / the glossary for the high-value,
   ambiguous columns (deliberate, not exhaustive). `recover_value_dims:` names dims to look up first.
2. **Low-cardinality value catalog** (opt-in, `value_catalog: {max_cardinality: 25}`) — the view's
   dimensions are profiled once (`DESCRIBE SEMANTIC VIEW`, Cortex only), those with ≤N distinct values
   are handed to **framing** as authoritative stored values, so the first try binds correctly.
   High-cardinality dims (names, ids) are skipped automatically.
3. **Auto recovery from the failed predicate** (always on) — on an empty result the engine reads the
   SQL Analyst actually ran, extracts its `col = 'literal'` / `col IN (...)` columns, resolves them to
   the view's dimensions, looks up their real values, and feeds the **reformulate** step. No list needed.
Mock packs degrade to (1) — the Cortex capabilities are duck-typed and optional.

## 6. Add a new agent — checklist

1. Copy `usecases/_TEMPLATE/` to `usecases/<your_pack>/` (includes `__init__.py`).
2. Edit `config.yaml`: `inherits: [shared]`, a `name`, a `sample_task`. Add `tools:` if it's not a
   single-SQL agent; add `exclude_shared_skills: [sql_safety]` if it's **not** a SQL agent. If it's an
   **AI+BI/Analyst pack**, also add `semantic_layer.yaml` (copy `_TEMPLATE/semantic_layer.example.yaml`)
   naming your native view/model — see section 5.
3. Write `prompts/generate.md` — the persona. Reference `{skills}`, `{exemplars}`, `{data}`/`{observations}`.
4. Add `skills/*.md` for domain rules (interpretation/presentation — see section 5). Only what differs
   from the shared skills.
5. Add `exemplars/*.yaml` — a handful of curated pairs (use `kind:` if you mix formats).
6. Override `prompts/rubric.md` **only if** this domain needs different scoring. If you do, keep the
   axis points summing to `{max_score}` — the rubric is not an auto-rescaler, so if you change
   `max_score`, rebalance the axes (shipped rubrics are three axes x /6 = 18).
7. Add `evals/golden_set.yaml` — normal, ambiguous, and unsupported cases. Prefer substring tokens the
   model won't reformat (a code like `SD-01`, not a number that may print as `25,475`).
8. Add `fixtures.py` with a `MockSQLTool` so it runs on mock with no creds.
9. Run it: `python run_local.py <your_pack>` (mock), then `python run_evals.py <your_pack>`.

**Non-SQL pack example:** `api_assistant` — declares `tools:` (a mock catalog + the generic http tool),
sets `exclude_shared_skills: [sql_safety]`, and uses `input`/`output` exemplars. Study it when building
an API-backed (non-SQL) agent.

---

## 7. Reference: how this maps to a native Cortex Agent

Not a spec to mirror — just a cross-walk for anyone familiar with the Snowflake Cortex Agent config UI:

| Cortex Agent (native UI) | portable-agent equivalent |
|---|---|
| General (name, description, example questions) | `config.yaml` (`name`, `sample_task`) |
| Instructions (response / planning) | `prompts/generate.md` + `skills/*.md` |
| Tools (Analyst / Search / custom) | `config.yaml` `tools:` + `engine/tools/` adapters |
| Tool resources — the Analyst semantic view / model | `semantic_layer.yaml` (Analyst/Genie packs) |
| Skills | `skills/*.md` |
| MCP | a tool type behind `engine/tools/` (future adapter) |
| Evaluations | `evals/golden_set.yaml` + the judge's `prompts/rubric.md` |
| Observability | `engine/tracing.py` events (run_id-keyed) |

We deliberately keep our own folder names (`prompts`/`skills`/`exemplars`/`evals`) rather than renaming
to match the UI: they're engine-wired and already industry-recognizable.

---

## See also
- [`the-refine-loop.md`](the-refine-loop.md) — the loop, prompts, `{placeholders}`, refine vs. rubric.
- [`end-to-end-flow.md`](end-to-end-flow.md) — which artifact runs when (runtime vs. test-time).
- [`evals-and-the-learning-flywheel.md`](evals-and-the-learning-flywheel.md) — evals grade, they don't train.
- [`tools-and-agents.md`](tools-and-agents.md) — the generic tool layer (SQL is one tool).
