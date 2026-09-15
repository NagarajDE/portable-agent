
# Plan: Hybrid discovery-feeds-framing

## Goal & decisions (from you)
- **Approach:** *Hybrid — discovery feeds framing.* At framing time, fetch the dimension's real distinct values at runtime and pass them into the framing prompt. No hand-maintained **value** lists (you still declare a short list of **column names**, which are stable).
- **Canonical "active":** `BUSINESS_STATUS IN ('Executed','Approved')` — **no** expiry-date filter. This matches the icertis view's baked-in rule and, crucially, keeps framing consistent with **Option-2 minimal** (no date predicate is ever added by framing).

## The idea in one line
Skills keep the *concept→column* binding and the *definition* of a business word (one stable line: `active = Executed + Approved`). **Discovery** supplies the *volatile value domain* (the actual `BUSINESS_STATUS` spellings) at runtime, so the worker (a) never invents a literal like `'active'`, and (b) binds any named value — a status, a category, a region — to its **exact stored spelling**. This generalizes beyond the one "active" bug: it also fixes casing/spelling mismatches (e.g. "consumables" → the stored `Consumables`).

## Why this over the alternatives
- vs. **static maps** (today): removes the brittle 14-value enumeration a human has to keep in sync; the engine reads the live domain instead.
- vs. **enrich the semantic view** (data-team synonyms/sample_values): that's the true root-cause fix and helps the native agents too, but it needs view ownership; this engine-side change we can ship now and it degrades gracefully if the view is later enriched.
- vs. **agentic re-query loop** (what the native agent does): equivalent effect, but a full "0 rows → introspect → reason → re-ask" loop adds round-trips and non-determinism. Front-loading the values into framing gets the same correctness with **one** Analyst call.

---

## Design

### 1. Tool capability: `distinct_values(...)` (engine/sql_tool.py)
Add an **optional** method to `CortexAnalystTool` (it already holds a live Snowpark session `self._s` and the view name):

```python
def distinct_values(self, dim_paths: list[str], limit: int) -> dict[str, list[str]]:
    # for each "TABLE.DIM", run:  SELECT * FROM SEMANTIC_VIEW(<view> DIMENSIONS <TABLE.DIM>) LIMIT <limit>
    # returns {dim_path: [values...]}; read-only, bounded, best-effort (skips a dim that errors)
```

- Uses the confirmed TVF form (`SEMANTIC_VIEW(view DIMENSIONS table.dim)` returns distinct values). For `procurement_contracts` the path is `BLV_ICERTIS_CONTRACT.BUSINESS_STATUS` (verified working earlier this session).
- **Not** added to the `SQLTool` Protocol — it's duck-typed. `MockSQLTool` and `GenieTool` do **not** implement it, so discovery **no-ops off-Cortex** (mock stays fully deterministic; golden/unit unaffected).
- Bounded `LIMIT` + read-only. No writes, single statement.

### 2. Config knobs (engine/graph.py, in `build_graph`)
- `frame_discover: true|false` — master toggle, **default OFF** via `strict_bool` (packs that don't set it are byte-for-byte unchanged).
- `frame_discover_dims: [ "<TABLE>.<DIM>", ... ]` — the low-cardinality categorical dimensions to enumerate. **Column-level** maintenance only.
- `frame_discover_limit: <int>` — cap values per dim (default 50, bounded like the other numeric knobs).
- Validation: `frame_discover_dims` must be a non-empty list of `TABLE.DIM` strings when `frame_discover` is on; `frame_discover_limit` an int in a sane range.

### 3. Framing wiring (engine/graph.py, `_frame_question` + closure)
- On the **first** framing call in a graph's life, lazily call `sql.distinct_values(dims, limit)` and **cache** the result in the closure (`_discovered = {}`) — paid once per session, reused for every later question.
- **Fail-safe** at every step: no `distinct_values` method (mock/genie), `frame_discover` off, empty dims, or **any exception/timeout** → discovered map is `{}` and we fall back to today's behavior (skill text only). Discovery can only *add* precision; it can never create an escalation or crash a run.
- Build a compact **KNOWN VALUES** block and pass it into `frame.md` via a new `known_values=` kwarg. `fill()` is brace-safe both directions (verified): an empty block renders as nothing; the kwarg is harmless even before the placeholder exists.

Block shape:
```
KNOWN VALUES (use the EXACT spelling shown; never invent a value not listed):
- BUSINESS_STATUS: Executed, Approved, Expired, Terminated, Cancelled, On Hold, Draft, ...
```

### 4. `shared/prompts/frame.md`
Add a `{known_values}` section and **one** rule, consistent with Option-2:
> When you bind a named value, match it to one of the KNOWN VALUES using its **exact** spelling. If the user's word (e.g. "active") is **not itself** a listed value, use the SKILLS' definition to choose the right listed value(s). **Never** invent a value that is not listed. Still add no filters/dates/scope the user didn't state.

Backward compatible: when `{known_values}` is empty the prompt reads exactly as it does today.

### 5. Skills — trim the volatile lists (keep the stable knowledge)
For `procurement_contracts`, `goa_spend`, `inventory_balance` `dimension_map.md`:
- **Remove** the enumerated raw value lists (e.g. the 14 `BUSINESS_STATUS` values) — discovery now supplies those.
- **Keep** the concept→column bindings, the measure guidance, and the **canonical one-liners**: `active`/`live` = `BUSINESS_STATUS IN ('Executed','Approved')`, `expired` = `'Expired'`; contract value = `UPDATED_CONTRACT_VALUE` else `CONTRACT_VALUE`; etc.

### 6. Rollout scope
Enable on the three value→column packs: **procurement_contracts** (the proven regression), **goa_spend** (IT Software → `ExecutiveCategory`), **inventory_balance** (`ProductType`). **icertis_procurement stays as-is** (its multi-table view is already instrumented with synonyms + an "active = Executed/Approved" custom instruction; adding an unqualified status map risks cross-table ambiguity).

Example (`usecases/procurement_contracts/config.yaml`):
```yaml
frame_query: true
frame_skills: [dimension_map.md]
frame_discover: true
frame_discover_dims: [ "BLV_ICERTIS_CONTRACT.BUSINESS_STATUS" ]
```
(Exact `TABLE.DIM` paths for goa/inventory confirmed via `DESCRIBE SEMANTIC VIEW` before wiring.)

---

## Guarantees (fail-safe & determinism)
- **Off by default** → unset packs render byte-identical; existing golden/tests unaffected.
- **Mock no-op** → `MockSQLTool` lacks `distinct_values`, so discovery is skipped; the 15/15 golden and the artifact-wiring tests stay deterministic.
- **Best-effort** → any discovery error/timeout falls back to skill text; the drift guards (`_keeps_subject`/`_keeps_pinned`) still police the framed output; injected values are advisory.
- **Bounded cost** → only pack-declared dims are enumerated (no auto-scan of high-cardinality supplier/material columns), each `LIMIT`-capped, cached once per session.

## Explicit v1 non-goal (surfaced for your call)
v1 uses **pack-declared** dim paths. A fully-automatic mode ("read view metadata, enumerate every dimension under a cardinality threshold") is possible but heavier (a `DESCRIBE` + N cardinality probes) and riskier (low-cardinality ≠ "a concept users name"). I recommend **deferring** it to an opt-in follow-on. If you'd rather go fully-automatic now, say so and I'll fold it into task 1–3.

---

## Verification
**Offline**
- New unit tests in `tests/test_grounding.py` using a fake tool that exposes `distinct_values`:
  1. KNOWN VALUES block appears in the **framing** prompt (`prompts[0]`) and **not** in generate/refine prompts.
  2. `frame_discover` off → `distinct_values` never called + byte-identical prompt.
  3. Tool without `distinct_values` (mock) → no-op, no crash.
  4. `distinct_values` raises → fallback to skill text, run still frames off the raw task.
  5. `frame_discover_limit` is passed through.
- `py -3 -W ignore -m pytest -q` stays green (296 + new); golden sweep 15/15 (discovery no-op on mock).

**Live (Cortex)**
- `procurement_contracts` "top 10 active contracts by value" → KNOWN VALUES shows `Executed/Approved/...` → framed to `BUSINESS_STATUS IN ('Executed','Approved')` → **grounded** (not `no_data`).
- A **spelling** case (a category/status value with tricky casing) → binds to the exact stored spelling.
- `goa_spend` control: "average invoice for the IT Software category" still → `ExecutiveCategory='IT Software'` → ~$21,339 (no regression).

## Docs & memory
- `docs/testing/test-campaign-results.md` — new **Round 7** (discovery-feeds-framing: design, offline + live results).
- `docs/concepts/use-case-pack-anatomy.md` — document `frame_discover` / `frame_discover_dims` / `frame_discover_limit`.
- `docs/concepts/end-to-end-flow.md` — add a discovery box feeding the FRAME step.
- Project `open_findings.md` — record the applied capability + rollout + the v1 non-goal.

All changes remain **uncommitted** on `9293118` for you to push.
