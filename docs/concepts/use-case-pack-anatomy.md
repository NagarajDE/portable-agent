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
- **config.yaml** — the knobs: `inherits: [shared]`, a human `name`, a `sample_task`, and optionally
  `tools:`, scoring (`max_score`, `pass_score`/`threshold`), `max_iters`, `eval_retries`, `max_stall`,
  and `exclude_shared_skills:` (section 4).
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

### Declaring the semantic layer (Analyst/Genie packs only)

An AI+BI pack names its native semantic object in `usecases/<pack>/semantic_layer.yaml`. The engine
reads it from the pack and passes it to the live text-to-SQL tool:

```yaml
snowflake:
  view: DB.SCHEMA.MY_SEMANTIC_VIEW              # a native Semantic View
  # model_file: "@DB.SCHEMA.STAGE/model.yaml"   # OR a semantic-model YAML on a stage (view wins if both)
databricks:                                     # optional; ready for when Genie is wired
  metric_view: main.schema.my_metric_view       # OR  genie_space: <id>
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
