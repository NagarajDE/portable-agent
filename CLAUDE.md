# Portable Agent — Project Memory for Claude Code

Read this before making changes. It captures the architecture decisions and the
reasoning behind them, so you don't re-litigate them per session.

## What this is

A LangGraph agent (generate → evaluate → refine, scored against an /N rubric)
built to survive a Snowflake ↔ Databricks migration without retraining or
rediscovering tuning. It runs on a MOCK provider today (zero credentials) and
swaps to real Cortex/Databricks/Anthropic via one env var each.

## The core principle (don't violate this)

> The model/platform is a replaceable component behind a stable interface.
> Everything valuable references the interface — never a vendor SDK directly.

Concretely: `engine/` never imports `anthropic`, `snowflake`, or `databricks`
directly except inside the one adapter class built for that vendor. If you're
about to add vendor-specific logic to `engine/graph.py`, stop — it belongs in
an adapter (`engine/llm_client.py` / `engine/sql_tool.py`) or a use-case pack.

## Three-tier architecture

```
engine/     GENERIC. shared code. touch rarely.
  graph.py             the loop: generate→evaluate→refine, composes shared+pack
  llm_client.py         LLMClient interface + mock|anthropic|cortex|databricks
  sql_tool.py           SQLTool  interface + mock|cortex-analyst|genie
  platform_databricks/agent.py   the ONLY Databricks-specific file (~25 lines)

shared/     COMMON conventions. inherited by every pack unless overridden.
  prompts/rubric.md      base /18 rubric        (pack overrides if domain needs it)
  prompts/refine.md      base self-correction
  skills/reporting.md    house reporting format  (every agent gets it)
  skills/sql_safety.md   SELECT-only safety      (every agent gets it)
  config.yaml            default threshold/max_iters/tool

usecases/<name>/     ONE folder = ONE agent. this is what you add per use case.
  config.yaml          inherits: [shared] + name + sample_task  (a few lines)
  prompts/generate.md  the persona — the one file that's truly per-agent
  prompts/rubric.md    ONLY if this domain needs different scoring than shared
  skills/*.md          ONLY domain-specific how-to (not reporting/safety — inherited)
  exemplars/*.yaml      verified Q→SQL pairs (your portable few-shot / seed)
  evals/golden_set.yaml your portable eval questions
  fixtures.py           MOCK-only demo data; ignored once SQL_TOOL/LLM_PROVIDER are real
```

### Composition rules (implemented in `engine/graph.py`)
- **skills**: `shared/skills/*.md` + `pack/skills/*.md` → CONCATENATED
- **prompts**: `pack/prompts/<f>` if it exists, ELSE `shared/prompts/<f>` → OVERRIDE
- **config**: `shared/config.yaml` merged with `pack/config.yaml` (pack wins) via
  `inherits: [shared]`

### Adding a new agent = adding a folder, never touching `engine/` or `shared/`
```
usecases/my_new_agent/
  config.yaml          inherits: [shared], name, sample_task
  prompts/generate.md  persona (+ {skills}{exemplars}{data} placeholders)
  exemplars/  evals/    your tuning
  # rubric.md / refine.md only if overriding shared
```

## Existing use-case packs (reference examples)

| pack | mode | rubric axes |
|---|---|---|
| `dq_qals` | is the data right? | rule correctness · evidence & quantification · actionability |
| `kpi_analytics` | what does the metric say? | metric-definition correctness · time grain & framing · clarity |
| `anomaly_rca` | why did it change? | hypothesis quality · evidence & isolation · actionable conclusion |
| `parity_hana_snowflake` | cross-engine parity | overrides the shared rubric (demonstrates override, not inherit) |

## Hard-won decisions from design discussion (don't re-derive these)

1. **"Fine-tuning" these agents ≠ training weights.** Improvement happens via
   config (instructions, verified queries, rubric) — the base LLM never
   retrains. This is why migration never requires retraining: only config
   moves, and config is just text files.

2. **The semantic layer does NOT port.** Snowflake Semantic Views and
   Databricks Metric Views are different, vendor-native formats. Accept
   rebuilding the semantic model per platform — this repo does not attempt to
   abstract that. (If true cross-platform semantic neutrality is ever
   required, the answer is a neutral layer like dbt Semantic Layer/MetricFlow
   or the emerging Open Semantic Interchange standard — not this loop.)

3. **Lock-in is prevented by OWNERSHIP, not just by the loop.** The loop
   preserves orchestration. It does nothing by itself for tuning that
   accumulates inside a vendor. That's why `exemplars/` and `evals/` exist:
   they're the git-owned home for verified Q→SQL pairs and golden questions
   that would otherwise pool inside Cortex/Genie. Rule of thumb: "own it or
   lose it" — only worth externalizing assets that are high-value and
   hard-to-rebuild (a handful of curated verified queries), not routine
   vendor telemetry.

4. **Verified queries ≠ every generated SQL.** `exemplars/*.yaml` holds a
   small, hand-curated set of Q→SQL pairs (tens, not thousands) — the ones
   worth blessing. Never log every runtime-generated query here; that's
   disposable and regenerates identically on any platform.

5. **Don't over-share into `shared/`.** Something belongs in `shared/` only
   when ≥2 packs genuinely want it identical (tone, SQL safety, base rubric
   shape). A pack's persona and domain skills stay in the pack even if that
   means some duplication — a bloated shared file with per-domain
   conditionals is worse than duplication.

6. **Cortex Agents / Genie are an alternative to this loop, not a base for
   it.** Using them means handing them orchestration instead of `engine/`.
   The clean bridge: expose Cortex Analyst / Genie AS a tool behind
   `SQLTool`, called BY this loop — don't try to build this loop "on top of"
   them.

7. **Build the loop only when it earns its keep**: cross-domain questions
   (needs a supervisor to call multiple domain agents-as-tools), your own
   eval/guardrails, or multi-step planning. For a single-domain BI Q&A agent,
   Cortex Analyst / Genie alone is often simpler and sufficient — don't
   over-engineer.

## Running it

```bash
pip install langgraph pyyaml
python run_local.py                        # dq_qals by default
python run_local.py kpi_analytics
python run_local.py anomaly_rca
python run_local.py parity_hana_snowflake
python run_evals.py <use_case>             # golden-set pass/fail
```

Flip to a real platform, no code change:
```bash
LLM_PROVIDER=cortex      SQL_TOOL=cortex   python run_local.py <use_case>
LLM_PROVIDER=databricks  SQL_TOOL=genie    python run_local.py <use_case>
LLM_PROVIDER=anthropic   SQL_TOOL=mock     python run_local.py <use_case>
```

## Real adapters are stubbed, not implemented

`engine/llm_client.py` (`CortexClient`, `DatabricksClient`) and
`engine/sql_tool.py` (`CortexAnalystTool`, `GenieTool`) have the exact SDK call
shape with a `TODO` at the one line needing real creds/endpoint. Wire one at a
time and test with `SQL_TOOL=mock` first to isolate LLM vs. SQL-tool issues.

## Deploying on Databricks

Recipe is the docstring at the top of `engine/platform_databricks/agent.py`:
MLflow Models-from-Code (`code_paths=["engine","shared","usecases"]`) → Unity
Catalog registration → `agents.deploy()` → Databricks App. That file is the
ONLY thing you rewrite if you ever leave Databricks.

## Ownership tiers (what moves on migration)

```
█ engine/ + shared/ + usecases/   yours, portable   → unchanged on any platform
▒ platform_databricks/            ~25-line glue     → the only file you rewrite
░ Cortex / Genie / Databricks     rented infra      → swapped for the new vendor's
⚪ semantic model + raw data       native            → rebuilt per platform (accepted)
```

## Conventions for this codebase specifically

- Prefer plain files (`.md`, `.yaml`) over code for anything domain-specific —
  that's what keeps a pack editable by a non-engineer and diffable in git.
- Prompt filling uses `fill()` (brace-safe string replace), not `str.format()`
  — SQL/JSON examples in prompts contain literal braces that would break
  `.format()`.
- Keep `agent.py` (the Databricks shell) free of business logic — it should
  only ever import and expose, never contain a prompt string or a rule.
- When editing a rubric or skill, prefer specificity to your actual domain
  (real thresholds, real table/column names) over generic placeholders — the
  shipped rubrics here are realistic starting points, not tuned to any one
  company's definitions.
