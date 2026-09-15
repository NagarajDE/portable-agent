# Multi-step reasoning — the `planned` mode (and how to pick a mode)

Some questions can't be answered by one retrieval: the answer to step 2 depends on what step 1 returned
("rank the top excess materials, THEN pull forward demand *for those materials*, THEN classify each").
`tool_mode: planned` handles that shape — the model commits a **validated plan (a DAG of read-only
steps) up front**, the engine executes it (chaining each step's result into the next), and the existing
`generate → evaluate → refine` loop synthesizes and scores the answer. Nothing about scoring, grounding,
refine, or tracing changes — planned is a new *gathering strategy*, not a new loop.

---

## 1. The three modes, and how a pack author chooses

`tool_mode` is one line in a pack's `config.yaml`. It picks how evidence is gathered before the answer
is written:

| Mode | Who picks the tools/queries | Adapts on success | Best for |
|---|---|---|---|
| `deterministic` (default) | the engine runs all declared read-only tools **once** | no | one retrieval has all the evidence |
| `agentic` | the model picks the next tool from the last observation (ReAct) | yes (free) | open-ended investigation whose path varies per question |
| `planned` | the model commits a **validated DAG**; the engine executes it; replans on failure | partial | a **known, repeatable** multi-step shape |

### The decision test
Ask: **"To write query N, do I need to have already seen the result of query N−1?"**

- **No** — one query (even a complex JOIN/window/CTE) gets everything → `deterministic` (or the legacy
  single-SQL path). *Don't reach for multi-step just because a question "sounds" complex — "top 10 vendors
  and their % of total" is one SQL.*
- **Yes, and the sequence is the same every time** (a report shape) → `planned`.
- **Yes, but the path varies per question / is exploratory** → `agentic`.

### You don't have to guess perfectly
Two things make the choice low-stakes:
1. **Mis-choosing fails *gracefully*.** A `planned` pack asked something trivial just emits a 1-step plan.
   A `deterministic` pack asked something too complex produces a shallower answer the judge scores lower —
   or, if the single query comes back blank, the grounding guard escalates rather than passing a
   fabricated answer. No mis-choice silently ships a wrong "passing" answer.
2. **Your golden-set evals confirm it empirically.** Write the questions the agent must answer; run
   `run_evals.py`. If `deterministic` can't hit the rubric on the multi-step questions, *that's the
   signal* to move to `planned`. Default to the simplest mode; step up only when the evals demand it
   (the repo's "build the loop only when it earns its keep" principle).

---

## 2. How `planned` runs

```
question
   │
   ▼  (inside the existing `generate` node, tool_mode == "planned")
run_planned:
   1. PLAN     model → JSON DAG → parse_plan()  [validates allowlist · deps · cycle · refs · budget]
                    invalid → one repair retry → still invalid → no_data (escalate)
   2. EXECUTE  first ready step (deps satisfied) → resolve {{sN}} → dispatch(tool, input, ctx)
                    step empty/failed → REPLAN (≤ max_replans; successful results FROZEN)
   3. RETURN   labeled observations "[s1 intent]\n…"  — or no_data(hint) if NOTHING succeeded
   │
   ▼
worker writes the answer from the observations (existing generate.md) → evaluate ⇄ refine → best answer
```

A plan is a list of steps, each `{id, intent, tool, input, dependencies}`. `intent` labels the step in
the observations (so the answer and a human can see the reasoning). `tool` must be one of the pack's
declared tools. To feed a previous result into a step, write `{{sN}}` in the input and list `sN` in the
step's dependencies.

### Reference chaining `{{sN}}` — and its one real risk
`{{sN}}` is replaced with a **bounded (≤600 char), single-line, sanitized summary** of step sN's output,
**single-pass** (a summary that itself contains `{{sM}}` is left literal — no recursion). Carry only
**compact facts** forward (ids, counts, thresholds), never whole tables. For the `sql` tool the summary
lands inside a natural-language question — so an untrusted prior result flows into the next query. That's
the deliberate trade for real chaining; it's mitigated by the bound, the untrusted-observation banner in
the plan prompt, and the fact that it only ever reaches another **read-only** SELECT behind `dispatch()`.

### Grounding — a planned run never fabricates a "passing" answer
A step whose result is the `NO_DATA` sentinel (or blank) is `empty`, **not evidence**. If **no** step
produces evidence, `run_planned` returns the `NO_DATA` sentinel → the run escalates (`grounded=False`,
judge never runs, `score=None`) exactly like the single-SQL path. Partial evidence → the answer is
written with a diagnostic and IS scored. Planned self-corrects via **replan**, so it's excluded from the
outer reformulate-retry loop and from the standalone framing step (the planner owns query formulation).

---

## 3. Security model

Planned's core new control is **deterministic validation before anything runs** — `parse_plan` rejects a
plan that names an undeclared tool, forms a cycle, references an undeclared step, or exceeds the step
budget, so a hallucinated or hostile plan can't execute.

| Surface | Control |
|---|---|
| The plan (model-generated) | validated by `parse_plan` **before** execution; tool must be in the pack's allowlist |
| Every step call | the same `dispatch()` boundary as all tools: input validated · **writes refused** (`approved=False`) · timeout · output bounded (20k) · errors redacted · never raises |
| A step's output feeding the next step | bounded/single-line/sanitized `{{sN}}` summary; single-pass; untrusted-observation banner; only into a read-only SELECT |
| Cost / DoS | `max_plan_steps` (≤20) caps dispatches; `max_replans` (≤5) caps revisions |
| Model-generated SQL | `_ensure_read_only()` + the deploy-time SELECT-only grant (the real boundary) |

**Injection posture, honestly:** stronger than `agentic` (deterministic validation + declared refs +
allowlist gate the model's choices) but weaker than pure `deterministic` (a result parameterizes a later
— still allowlisted, still read-only — query). Exfiltration is bounded: no write auto-runs, the `http`
tool is allowlisted, outputs are size-capped. Residuals (accepted): a poisoned data value flowing into a
downstream SELECT (bounded + banner + read-only, not eliminated); the `sql`-into-question chaining trade.

---

## 4. Config, prompts, tracing

```yaml
tool_mode: planned
tools:
  - type: sql            # (or mock/http/mcp) — the plan's allowlist = the declared tool labels
max_plan_steps: 8        # cap ~20; also the dispatch budget
max_replans: 2           # cap ~5
plan_skills: [ ... ]     # OPTIONAL: the decomposition guidance the PLANNER sees (keeps the plan prompt lean)
```

Prompts (shared, pack-overridable by file presence): `shared/prompts/plan.md`, `shared/prompts/replan.md`.
Synthesis reuses the pack's normal `generate.md`.

Per-step **tracing** (auditability is the point of a plan): `run_planned` emits `plan_created`,
`plan_step{id,tool,status,ms}`, `replan{n}`, and `replan_failed{reason}` events via `engine/tracing.py`
(fail-safe, no-op when `TRACER=none`), joined to the run's `generate/evaluate/refine` events by `run_id`.

**Reference hand-off (`max_ref_chars`, default 2000):** each `{{sN}}` is a bounded summary; a top-N list
of ids fits comfortably, and if the bound ever bites it truncates **visibly** (`…(+N chars truncated)`)
rather than silently dropping the tail. Raise it (≤8000) for packs that chain large lists, or have the
planner carry only compact ids.

**Cost & latency (important for broad questions).** Synthesis time scales with the *size of the gathered
evidence*, and a broad, unscoped multi-step question ("top excess across ALL plants") can make the
synthesis model slow — this is largely inherent to the question (a native single-shot agent is slow on it
too), not a defect of the loop. Mitigations, cheapest first: **scope the question** (one plant + top-N —
this reliably brings a >minutes run down to ~a minute); keep each step's rows bounded (`SQL_MAX_ROWS`);
and if you routinely ask broad questions, point the pack's worker at a faster model via `models:`. The
step budget (`max_plan_steps`) and per-tool output bound already cap how much evidence accumulates.

---

## 5. Example packs

- **`ops_rca`** — a creds-free, **non-SQL** planned agent (mock tools): detect the failing service →
  pull its health → correlate recent deploys → attribute a cause. Proves planned is not BI-only; runs
  offline with a real/scripted planner (the bare mock can't emit a plan, so it escalates).
- **`inventory_excess_disposition`** — a **SQL/BI** planned agent over `INVENTORY_ANALYTICS`: rank
  excess by value → forward demand for those materials (`{{s1}}`) → classify Hold / Reduce-inbound /
  Redistribute / Disposition. Offline on mock fixtures; live with `SQL_TOOL=cortex`.

Because planned lives in `engine/`+`shared/` and drives tools through `dispatch()`, a planned pack
migrates Snowflake↔Databricks with no loop change — the plan, prompts, `plan_skills`, and golden set are
git-owned text; only the semantic model is rebuilt per platform.

---

## 6. Related

- The generate→evaluate→refine loop and the three "try again" moments: [the-refine-loop.md](the-refine-loop.md),
  [retries-explained.md](retries-explained.md).
- The tool layer, `dispatch()`, deterministic vs agentic gathering: [tools-and-agents.md](tools-and-agents.md).
