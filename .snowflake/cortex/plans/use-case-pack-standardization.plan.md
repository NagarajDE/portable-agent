# Use-case pack standardization + authoring docs

## 0. Direct answers to your questions (documented, not just chat)

**Is `generate.md` static or dynamically populated?**
Both, in different senses. The **file is static** — you author it once during agent development and it lives in git; it does not change per run. What's dynamic is only the `{placeholders}` inside it: at runtime the engine's `fill()` swaps `{task}`, `{data}`, `{skills}`, `{exemplars}`, `{revision}` for live values. So: *static template, runtime-filled slots.* Same for `rubric.md` and `refine.md`.

**To override the shared `refine.md` for one pack, do I mention it in config?**
No. Overrides work **by file presence, not config**. If `usecases/<pack>/prompts/refine.md` exists, the engine uses it; otherwise it falls back to `shared/prompts/refine.md` (`_prompt()` in `engine/graph.py`). You do **not** list prompts in `config.yaml`. The `inherits: [shared]` key controls **only** `config.yaml` merging — nothing else.

**What merges vs. overrides vs. concatenates (the "one place" table):**

| artifact | shared default? | rule | how it's triggered |
|---|---|---|---|
| `config.yaml` | yes | **INHERIT + MERGE** (pack wins) | `inherits: [shared]` in pack config |
| `prompts/generate.md` | no (pack-only) | pack-owned; no shared default | file present in pack |
| `prompts/rubric.md` | yes | **OVERRIDE** (pack ELSE shared) | by **file presence** — no config |
| `prompts/refine.md` | yes | **OVERRIDE** (pack ELSE shared) | by **file presence** — no config |
| `skills/*.md` | yes | **CONCATENATE** (shared + pack) | both dirs globbed, sorted |
| `exemplars/*.yaml` | no | pack-only, globbed | all `*.yaml` in `pack/exemplars` |
| `evals/golden_set.yaml` | no | pack-only (test-time) | `run_evals.py` reads it |
| `fixtures.py` | no | pack-only (mock data) | Python import |

## 1. Folder-structure decision: KEEP the layout, standardize + document it

**Is `rubric.md` correctly under `prompts/`?** Yes — keep it. `rubric.md` is a runtime prompt template (the judge's prompt), the same class as `generate.md` and `refine.md`; `prompts/` = "the three loop templates `fill()` populates." Moving it into `evals/` would be actively misleading, because `evals/` is **test-time only** while the rubric runs on **every live question**.

**Considered and rejected — renaming folders to mirror the native Cortex tabs** (`instructions/`, `tools/`, `evaluation/`): it would touch `engine/graph.py`, `run_evals.py`, `tests/`, and all 6 packs for near-zero functional gain, and "refine" has no native analog. Our names (`prompts` / `skills` / `exemplars` / `evals`) are already industry-recognizable. Instead of renaming, I'll add a **mapping table** so anyone who knows Cortex Agents feels at home:

| Cortex Agent (native UI) | portable-agent equivalent |
|---|---|
| General (name, description, example questions) | `config.yaml` (`name`, `sample_task`) + pack `README.md` |
| Instructions (response/planning) | `prompts/generate.md` + `skills/*.md` |
| Tools (Analyst / Search / custom) | `config.yaml` `tools:` + `engine/tools/` adapters |
| Skills | `skills/*.md` |
| MCP | a tool type behind `engine/tools/` (future adapter) |
| Evaluations | `evals/golden_set.yaml` + the judge's `prompts/rubric.md` |
| Observability | `engine/tracing.py` events (run_id-keyed) |

**Industry practices applied:** scaffold/template pattern (cookiecutter), per-component README ("agent card", like model cards), convention-over-configuration (file-presence overrides, à la Next.js/Rails), and keeping a stable public contract (folder names) to avoid churn.

## 2. Standard skeleton (what each folder must contain)

```
usecases/<pack>/
  __init__.py            REQUIRED  — makes the pack importable (fixtures.py)
  config.yaml            REQUIRED  — inherits: [shared], name, sample_task; optional tools:
  prompts/
    generate.md          REQUIRED  — the persona; the one truly per-agent prompt
    rubric.md            optional  — ONLY to override the shared judge rubric
    refine.md            optional  — ONLY to override the shared refine instructions
  skills/*.md            optional  — domain how-to; concatenated AFTER shared skills
  exemplars/*.yaml       optional  — few-shot: verified Q→SQL or input→output
  evals/golden_set.yaml  recommended — test-time regression questions
  fixtures.py            REQUIRED (mock) — canned rows so it runs with no creds
  README.md              recommended — the "agent card": what/why/data/rubric/sample
```

## 3. Concrete changes

**Create — authoring guide**
- `docs/concepts/use-case-pack-anatomy.md`: the skeleton above (required vs optional), a per-file "what goes here" guide, the composition table from §0, the "override is by file presence, not config" callout, the native-taxonomy mapping table, and a "add a new agent in 4 steps" quickstart (copy `_TEMPLATE` → edit `config.yaml` → write `prompts/generate.md` → add `fixtures.py` rows).

**Create — copy-to-start scaffold (runnable on mock out of the box)**
- `usecases/_TEMPLATE/` with: `__init__.py`, `config.yaml` (inherits shared, name, sample_task), `prompts/generate.md` (persona showing every placeholder), `skills/example_skill.md`, `exemplars/examples.yaml`, `evals/golden_set.yaml`, `fixtures.py`, and `README.md`. It deliberately **omits** `rubric.md`/`refine.md` to demonstrate inherit-from-shared, with the README explaining how to add them to override. Leading underscore signals "not a real pack"; nothing auto-discovers it.

**Create — pack index**
- `usecases/README.md`: one-line entry per pack + link to the anatomy doc and `_TEMPLATE`.

**Create — per-pack "agent cards" (optional; say the word to skip)**
- A short `README.md` in each of the 6 existing packs (name, mode, data source, rubric axes, sample task). Additive, no code impact.

**Fix minor drift**
- Add `usecases/api_assistant/__init__.py` if it's genuinely missing (verify first).
- Document the exemplar-filename convention (`verified_queries.yaml` for SQL packs, `examples.yaml` for tool/non-SQL packs) — both are globbed, so no forced rename.

**Cross-link**
- Add the anatomy doc to the README docs-pointer block.
- Update the `CLAUDE.md` folder section to point at the anatomy doc and state the override-by-presence rule explicitly.

## 4. Risk & verification
- **No folder renames** → `engine/graph.py`, `run_evals.py`, and the `fixtures` import are untouched → the 185 existing tests stay valid.
- Before finishing: confirm no test dynamically iterates `usecases/` (grep for `iterdir`/`glob` over `USECASES`); if any exists, exclude names starting with `_`.
- Run `py -3 -W ignore -m pytest -q` → expect **185 passed**.
- Optionally `python run_local.py _TEMPLATE` on the mock provider to prove the scaffold runs end-to-end.

## Out of scope
- No engine changes, no folder renames, no behavior changes to the loop.
- Not touching the native `INVENTORY_ANALYSIS_AGENT`.