---
name: "multi step reasoning"
created: "2026-09-15T01:57:56.252Z"
status: pending
---

# Plan: multi-step reasoning for analytical packs

## The key finding (why this is "turn on + harden", not "build")

The engine already has the bones of exactly what you pictured:

- **`run_agentic`** (engine/tools/agentic.py) is a bounded ReAct loop that **accumulates results across steps** — the model emits `{"tool":…}`, the engine runs it, appends the observation, feeds all observations back, and loops until `{"final":true}` or the step budget. That IS "fetch, then decide the next fetch, statefully."
- **`SqlBridgeTool`** (engine/tools/sql\_bridge.py) already exposes text-to-SQL as a callable `sql` tool (`input: {"question": …}`), so the model can issue **many** NL sub-questions to Cortex Analyst and chain them.
- The trust boundary (`dispatch`), tool loading (`load_tools`), and a working agentic pack (`incident_triage`, `tool_mode: agentic`) already exist.

So a multi-step inventory pack is: `tool_mode: agentic` + `tools: [{type: sql}]`. The reason it doesn't work well today is three concrete gaps.

## Plain-language flow (after this change)

```
your question
  → [gather loop, up to N steps]      the model, guided by the pack's workflow recipe, asks Analyst
      step 1: rank excess positions          sub-question → rows → observation (kept)
      step 2: forward supply/demand (2Q)      sub-question → rows → observation (kept)
      step 3: cross-plant demand              sub-question → rows → observation (kept)
      step 4: planners for those materials    sub-question → rows → observation (kept)
      → {"final": true}
  → [synthesize] one answer: the bucketed report, from ALL observations
  → [evaluate] judge scores it → [refine wording if low]
```

## The three gaps to close (each grounded in code)

1. **Gathering is domain-blind.** `act.md` only sees `{task}`, `{tools}`, `{observations}` — NOT the pack skills. So the model doesn't know the excess-disposition recipe (which sub-queries to run, the INS exclusion, the classification rule). It gathers blindly. → Inject the pack's workflow skills into `act.md`.
2. **The agentic path skips our grounding/escalation.** In `generate()`, `can_retry = use_sql or tool_mode == "deterministic"` — agentic is excluded; and `run_agentic` returns the string `"No tool observations."` (not the `no_data` sentinel), so `grounded` stays `True` even when nothing was gathered. A blank multi-step run would score an empty answer instead of escalating. → Make `run_agentic` return `no_data` on an empty gather and wire the agentic path into the existing escalation.
3. **It can't be tested offline.** The mock LLM can't emit tool-call JSON, so on MOCK the loop is skipped (see the note in `incident_triage/config.yaml:19`). That means no unit/golden coverage for multi-step packs. → Teach `MockClient` to replay a scripted ReAct sequence so the loop is deterministically testable (closes the long-standing G5 gap).

## Design (recommended: harden the flexible agentic path)

1. **Domain-aware gathering** — add a `{skills}` placeholder to `shared/prompts/act.md` and pass the pack's workflow skills (a `frame_skills`-style subset is fine) into `run_agentic`. Now the model plans the *right* sub-queries. `act.md` stays domain-neutral; the domain recipe lives in the pack skill (consistent with the shared-layer-neutral rule).
2. **Grounding on the agentic path** — `run_agentic` returns the `no_data` sentinel when no usable evidence was gathered; `generate()` treats an agentic blank like a blank SQL retrieval (escalate `no_data`, judge skipped). Optionally allow ONE bounded re-plan before escalating.
3. **Offline testability** — extend `MockClient` to emit a configured sequence like `[{"tool":"sql","input":{"question":"…"}}, {"final":true}]`, so a multi-step pack runs deterministically on mock for unit + golden tests. Keeps mock hermetic.
4. **Depth + synthesis** — raise `max_tool_steps` for analytical packs (e.g. 6-8); keep the existing dedup / read-only / timeout bounds. Strengthen the pack's `generate.md` + add an **output-template skill** so the synthesis produces the bucketed report; extend the pack `rubric.md` to grade the multi-part structure and the "don't double-count overlapping buckets" caveat.
5. **Pack scaffolding** — configure the analytical pack (`inventory_balance` or a dedicated variant) with `tool_mode: agentic` + `tools: [{type: sql}]`; **restore the rich workflow skills** (the SQL patterns + output template the concise Round-6 skills dropped) IN THE PACK; fix the `'Instruments'` → `'Instrument'` literal bug in `excess_stock_disposition.md`.
6. **Data-coverage prerequisite** — the view already models max level, excess value, storage location, blocked stock, planner email, forecast demand, planning family. Verify it also models **inbound MRP supply** (planned orders/POs) and supports **cross-plant demand** lookups; if a piece is missing, that is a Layer-3 **semantic-view enrichment** dependency (data team) — flag it, don't hack around it.

## Alternative (note, not recommended for v1): deterministic "recipe" pipeline

For a reliability-critical, repeatable report we could instead have the pack declare an **ordered, dependent** sequence of parameterized sub-queries that the engine runs deterministically (extending the deterministic gatherer to chain steps), then synthesize. Pros: fully deterministic, easy to test, mirrors how the native skills encode exact SQL. Cons: only pre-authored workflows, more authoring, and it's more *new* machinery (chained steps don't exist yet). Recommend as a follow-on once the flexible path proves the capability.

## Honest expectations & tradeoffs

- This delivers the **capability** (genuine multi-step reasoning), demonstrated on the excess-disposition question. Exactly reproducing the native agent's formatted report also depends on the restored skills and the view coverage above — treat pixel-parity as a stretch goal, not the bar.
- Multi-step = **more Analyst calls** (latency + cost) and **less determinism** than a single query. The single-query path stays the default for simple questions; only analytical packs opt into multi-step.
- The agentic path's documented trust trade-off (tool output can influence later tool choice) is acceptable here — internal analytics over a trusted semantic view, every call still read-only through `dispatch`.

## Verification

- **Unit** (tests/): `act.md` receives skills; `run_agentic` returns `no_data` on empty and the run escalates; `MockClient` drives a scripted multi-step sequence; single-query packs unchanged (byte-identical prompts).
- **Golden (mock)**: a multi-step pack case runs deterministically to a synthesized answer; existing 16/16 unaffected.
- **Live (Cortex)**: the excess-disposition question on plant 3310 issues multiple Analyst sub-queries and returns a bucketed, grounded report approximating the native structure; a simple single-query question on another pack is untouched.
- **Non-regression**: `py -3 -W ignore -m pytest -q` stays green; the three single-query packs behave exactly as before.

## Critical files

- engine/tools/agentic.py — ReAct loop: add skills to the prompt render, return `no_data` on empty.
- shared/prompts/act.md — add a domain-neutral `{skills}` slot.
- engine/graph.py — bring the agentic path under the grounding/escalation model.
- engine/llm\_client.py — `MockClient` replays a scripted ReAct sequence (offline testability).
- usecases/inventory\_balance/ — `config.yaml` (agentic + sql tool), restored workflow + output-template skills, the `'Instrument'` fix.
