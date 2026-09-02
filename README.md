# Portable Agent

Three tiers: a generic **engine**, a **shared** conventions layer, and thin
per-agent **use-case packs**. A new agent = a persona + its own tuning; everything
mechanical and conventional is inherited. Switch model/database with one env var.
Host on Databricks with one shell file.

## Layout

```
engine/                       ← GENERIC. shared code. touch rarely.
  graph.py                    loop + tier composition (inherit / override / concat)
  llm_client.py               LLMClient + mock|anthropic|cortex|databricks
  sql_tool.py                 SQLTool  + mock|cortex-analyst|genie
  platform_databricks/agent.py   the ONLY Databricks-specific file

shared/                       ← COMMON conventions. inherited by packs.
  prompts/rubric.md           base /18 rubric        (pack can override)
  prompts/refine.md           base self-correction   (pack can override)
  skills/reporting.md         house reporting format (every agent gets it)
  skills/sql_safety.md        SELECT-only safety     (every agent gets it)
  config.yaml                 default threshold / max_iters / tool

usecases/                     ← one folder per agent. touch often.
  dq_qals/                    INHERITS shared rubric+refine; adds 1 domain skill
    config.yaml                 inherits: [shared] + name + sample_task
    prompts/generate.md         persona (the ~1 file that's truly per-agent)
    skills/duplicate_detection.md   domain-specific only
    exemplars/verified_queries.yaml verified Q→SQL (few-shot, portable seed)
    evals/golden_set.yaml       domain golden questions
    fixtures.py  config.yaml
  parity_hana_snowflake/      OVERRIDES rubric (keeps its own); inherits refine
```

## How the tiers compose

```
  skills   = shared/skills/*        +  pack/skills/*         (CONCATENATE)
  prompts  = pack/prompts/<f>       ELSE shared/prompts/<f>  (pack OVERRIDES)
  config   = shared defaults        +  pack config           (pack WINS; via inherits:)
```

Example in this repo:
- `dq_qals` has no `rubric.md` → uses `shared/prompts/rubric.md` (inherited).
- `parity` has its own `rubric.md` → uses it (override). Both inherit `refine.md`.
- Both get shared `reporting` + `sql_safety` skills, plus their own domain skills.

## What's common vs. rebuilt per agent

```
  Build once (engine + shared):  loop · adapters · eval runner · hosting shell
                                 · base rubric · reporting/safety skills · defaults
  Rebuild per agent:             persona (~1 paragraph) · semantic-view ref
                                 · verified Q→SQL · golden questions · metrics
```

A new agent's real cost: one persona file + its own tuning. Everything else inherited.

## Run it now (no credentials)

```
pip install langgraph pyyaml
python run_local.py                        # dq_qals: 12→14→16→18
python run_local.py parity_hana_snowflake
python run_evals.py                        # golden set pass/fail
python run_evals.py parity_hana_snowflake
```

## Flip to a real platform (one env var each — no code change)

```
All Snowflake     LLM_PROVIDER=cortex      SQL_TOOL=cortex   python run_local.py
All Databricks    LLM_PROVIDER=databricks  SQL_TOOL=genie    python run_local.py
Claude direct     LLM_PROVIDER=anthropic   SQL_TOOL=mock     python run_local.py
```

## Add a new agent = add a folder

```
usecases/my_new_agent/
  config.yaml         inherits: [shared] + name + sample_task
  prompts/generate.md persona (+ {skills}{exemplars}{data})
  exemplars/  evals/  (skills/ only for domain-specific rules)
  # rubric.md / refine.md ONLY if you need to override the shared ones
python run_local.py my_new_agent
```

## Host on Databricks

Docstring atop `engine/platform_databricks/agent.py`: MLflow Models-from-Code
(`code_paths=["engine","shared","usecases"]`) → Unity Catalog → `agents.deploy()`
→ Databricks App.

## Ownership tiers

```
█ engine/ + shared/ + usecases/   yours, portable  → unchanged on any platform
▒ platform_databricks/            ~25-line glue    → the only file you rewrite to move
░ Cortex / Genie / DBX            rented infra     → swapped for the new vendor's
⚪ semantic model + data           native           → rebuilt per platform (accepted)
```
