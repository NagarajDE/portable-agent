# Portable Agent — Project Context & Goals

This document captures **why this project exists, what we're trying to achieve,
and every design decision we made** — the full narrative. `CLAUDE.md` is the
operational spec (how to work in the code); this file is the reasoning behind it.

---

## 1. The goal (north star)

> Build a **portable, vendor-agnostic agentic platform** so that agents — and the
> tuning accumulated into them over time — are **not locked into Snowflake or
> Databricks**. Migrating between platforms should mean *swap an adapter + rebuild
> the semantic layer*, never *retrain from scratch*.

Success = the day we move platforms, we keep: the orchestration loop, prompts,
skills, rubric, verified Q→SQL pairs, and golden eval sets — unchanged.

---

## 2. Why we started (the original question)

The session opened with a worry:

> "When we migrate agents to a new platform, do we lose all the trained/accumulated
> info? Do we have to retrain them? e.g. agents built on Snowflake or Claude Code,
> then moving to Databricks."

Everything below is the answer to that question, worked out in stages.

---

## 3. Core realizations (concepts we clarified)

| Assumption going in | What's actually true |
|---|---|
| Agents are "trained" and migration loses that training | Agents improve via **config** (prompts, verified queries, rubric), not model weights. The base LLM never retrains. |
| Migrating = retrain the model | Nothing to retrain. Swap the inference call; the model is an API either way. |
| Embeddings are "trained in" | An embedding is text → a vector on one model's "map". Migrating to a different embedding model = **re-embed the corpus** (a batch job), not retrain. |
| The semantic view will carry over | It won't. Cortex Semantic Views ≠ Databricks Metric Views — different native formats. **Rebuild per platform** (accepted, like migrating data). |
| A portable loop prevents lock-in | Only half of it. Lock-in is prevented by **ownership** — where your tuning physically lives — not by the loop alone. |
| There's a "fine-tune the agent" button | For BI agents, improvement = **curation** (semantic model + verified queries + instructions), which is portable if you own it, locked-in if the vendor hosts it. |

---

## 4. What we're building (the solution shape)

A LangGraph loop (generate → evaluate → refine, scored on an /18 rubric) sitting
behind stable interfaces, so the model and the database are swappable components.

```
        ┌─────────────────────────────────────────────┐
        │  YOUR AGENT  (portable)                       │
        │  loop · prompts · skills · rubric · exemplars │
        │  talks ONLY to interfaces, never a vendor SDK │
        └───────────────┬───────────────┬──────────────┘
                        │               │
                  LLMClient          SQLTool
                        │               │
                        ▼               ▼
            Cortex / Databricks     Cortex Analyst / Genie
            / Anthropic / Ollama    (text-to-SQL as a tool)
```

Switch provider = one env var. Switch database tool = one env var. The loop never
changes.

---

## 5. The three-tier architecture

| Tier | Contains | Shared? | Lives in |
|---|---|---|---|
| **Engine** | loop, adapters, eval runner, hosting shells | 100% shared | `engine/` |
| **Shared** | base rubric, refine prompt, reporting + SQL-safety skills, config defaults | inherited by packs | `shared/` |
| **Use-case pack** | persona, domain skills, verified Q→SQL, golden set, config | one per agent | `usecases/<name>/` |

Composition rules (in `engine/graph.py`):

| Asset | How it composes |
|---|---|
| skills | `shared/skills/*` **+** `pack/skills/*` (concatenate) |
| prompts | `pack/prompts/<f>` if present, **else** `shared/prompts/<f>` (override) |
| config | shared defaults **+** pack config, pack wins (`inherits: [shared]`) |

**Adding a new agent = adding a folder.** Never touch `engine/` or `shared/`.
Real cost of a new agent: one persona paragraph + its own tuning.

---

## 6. Decision log (the "why", so we don't re-argue these)

1. **Improvement is config, not weights** → migration never needs retraining.
2. **Semantic layer does not port** → rebuild per platform is accepted; this repo
   does not try to abstract it. (True neutrality, if ever required, = dbt Semantic
   Layer / MetricFlow or the emerging Open Semantic Interchange standard — not this loop.)
3. **Lock-in is prevented by ownership** → `exemplars/` and `evals/` exist so
   verified Q→SQL and golden questions live in git, not pooled inside Cortex/Genie.
   Rule: "own it or lose it" — externalize only high-value, hard-to-rebuild assets.
4. **Verified queries ≠ every generated SQL** → `exemplars/` is a small hand-curated
   set (tens, not thousands). Never log runtime-generated SQL there; it's disposable.
5. **Don't over-share into `shared/`** → something goes there only when ≥2 packs want
   it identical. Persona and domain skills stay per-pack even if it means duplication.
6. **Cortex Agents / Genie are an alternative to the loop, not a base for it** →
   using them = handing them orchestration. The bridge: expose them AS a tool behind
   `SQLTool`, called BY the loop.
7. **Build the loop only when it earns its keep** → cross-domain questions, your own
   eval/guardrails, or multi-step planning. Single-domain BI Q&A is often better served
   by Cortex Analyst / Genie alone. Don't over-engineer.
8. **Two hosting shells, architecturally parallel** → deploy to whichever platform;
   the engine/shared/usecases are identical. Adding a platform = one more
   `platform_<name>/` folder.

---

## 7. What exists now (current state)

| Component | Status |
|---|---|
| Engine loop (LangGraph, generate→evaluate→refine, /18) | ✅ built, runs on mock |
| LLM adapters: mock ✅ · anthropic/cortex/databricks | ✅ mock; others stubbed (SDK shape + TODO) |
| SQL-tool adapters: mock ✅ · cortex-analyst/genie | ✅ mock; others stubbed |
| Shared tier (rubric, refine, reporting + sql_safety skills) | ✅ built |
| Use-case packs | ✅ `dq_qals`, `kpi_analytics`, `anomaly_rca`, `parity_hana_snowflake` |
| Exemplars (verified Q→SQL) + golden eval sets per pack | ✅ built, `run_evals.py` green (2/2 each) |
| Databricks shell (`platform_databricks/agent.py`) | ✅ MLflow ResponsesAgent recipe |
| Snowflake shell (`platform_snowflake/`) | ✅ FastAPI + Dockerfile + spec.yaml, verified (/healthz 200, /invoke 18/18) |
| `CLAUDE.md` (Claude Code project memory) | ✅ built |

### The three analytics modes we built

| Pack | Question it answers | Rubric axes (each /6) |
|---|---|---|
| `dq_qals` | Is the data right? | rule correctness · evidence & quantification · actionability |
| `kpi_analytics` | What does the metric say? | metric-definition correctness · time grain & framing · clarity |
| `anomaly_rca` | Why did it change? | hypothesis quality · evidence & isolation · actionable conclusion |

---

## 8. Deployment targets

| Target | What it is | Fits when |
|---|---|---|
| **Databricks Apps** | Model Serving + MLflow ResponsesAgent | you're on Databricks |
| **Snowflake SPCS** | containers inside Snowflake | you're on Snowflake |
| **Any cloud container** | Cloud Run / ECS / AKS / your own k8s | multi-cloud, no vendor lock at all |
| **On-prem / VM** | plain Docker on a server you run | regulated, air-gapped, or an existing box |

Same engine every time; only the `platform_<name>/` shell changes.

---

## 9. What is intentionally NOT solved (scope honesty)

- **Semantic-layer portability** — rebuilt per platform, by design. Not abstracted here.
- **Real adapters** — Cortex/Databricks/Genie adapters are stubbed with the exact SDK
  shape + a `TODO`; not wired to live creds.
- **`spec.yaml` image URL** — placeholder `<repo_url>`; filled per Snowflake account.
- **Databricks catalog/schema names** — filled per workspace.

---

## 10. Open items / next steps

| Next step | Why it matters |
|---|---|
| Extract real Cortex verified queries → `exemplars/*.yaml` | Converts today's Snowflake-pooled tuning into portable git assets |
| Wire ONE real adapter end-to-end (e.g. Databricks Foundation Model) | Watch the loop run on a real model, not the mock |
| Add Streamlit-in-Snowflake chat UI | Gives the SPCS path a "looks like Genie" front end |
| Sync the GitHub remote | Remote lagged the local build (missing CLAUDE.md, 2 packs, platform_snowflake, etc.) |
| (If portability becomes board-level) neutral semantic layer via dbt/OSI | The only real lever for cross-platform semantic reuse |

---

## 11. How the pieces map to the goal

| Asset | Portable? | Where it lives |
|---|---|---|
| reasoning loop, adapters, eval runner, hosting shells | ✅ | `engine/` |
| base rubric, reporting/safety skills, config defaults | ✅ | `shared/` |
| persona, domain skills, verified Q→SQL, golden set | ✅ (owned in git) | `usecases/<name>/` |
| the platform shell | ▒ rewrite one small file to move | `engine/platform_<name>/` |
| Cortex / Genie / Databricks runtime | ░ swapped per vendor | (rented infra) |
| semantic model + raw data | ⚪ rebuilt per platform (accepted) | (native to each platform) |

**Bottom line:** everything that isn't the semantic layer is portable — and the
semantic layer we rebuild deliberately, the same way we migrate data.
