# RCA — planned-mode multi-step (`inventory_excess_disposition`)

**Date:** 2026-09-15
**Feature under test:** `tool_mode: planned` (validated-DAG multi-step reasoning) in `engine/tools/planned.py`, wired into the existing `_retrieve` seam in `engine/graph.py`.
**Trigger question:** *"Identify the specific open supply transactions (POs and planned orders) to defer or cancel for the 8 'Reduce Replenishment' items."*
**Environment:** worker `cortex:claude-opus-5`, judge `cortex:claude-opus-4-8`, live `OPERATIONS_DEV.OPERATIONS_SANDBOX.INVENTORY_ANALYTICS`, `TRACER=stdout`.

Both findings below are independent of the (by-design) lack of cross-turn memory — the framework is stateless per CLI turn.

---

## BUG-1 — Replan rejects a dependency on an already-succeeded (frozen) step — **High, code defect, FIXED (2026-09-15)**

> **RESOLVED.** `parse_plan` now takes `known_ids` and validates dependencies against `idset | known_ids`
> (`engine/tools/planned.py`); `_ask_once`/`_plan_with_repair` thread it through, and the replan call in
> `run_planned` passes the frozen SUCCESSFUL ids (`frozen_ids`). Initial-plan behavior is unchanged
> (`known_ids=frozenset()`). No executor change. Regression tests:
> `tests/test_planned.py::test_parse_plan_accepts_dependency_on_a_frozen_step` (unit) and
> `::test_replan_can_depend_on_a_frozen_step` (end-to-end: a replan that omits the frozen s1 but still
> depends on it now validates, reuses s1's result, and recovers s2).
>
> **Verified live 2026-09-15.** Offline `pytest` **334 passed** (+3 vs the prior 331 — the regression tests above). Live re-run of the exact original failing question → **two clean `replan` events, zero `replan_failed`**, grounded (score 15) in 138s, and the answer honestly opens by flagging the snapshot/data limitation. The proven happy-path 3-step chain is unaffected (grounded, score 18, 90s). The original report follows.

**Symptom (trace):**
```
plan_step   id=s1  status=success
plan_step   id=s2  status=empty
replan_failed n=1  reason: "step s2 depends on unknown step 's1'"
replan_failed n=2  reason: "step s4 depends on unknown step 's1'"
```

**Root cause — validation is scoped to the *current* plan's step ids only:**
- `engine/tools/planned.py:162` → `idset = set(ids)`  (ids of the **new** plan only)
- `engine/tools/planned.py:171-173`:
  ```python
  for d in s.dependencies:
      if d not in idset:
          raise PlanError(f"step {s.id} depends on unknown step {d!r}")
  ```

On a replan (`run_planned`, `engine/tools/planned.py:319-336`), the succeeded steps are frozen into `results`/`summaries` and `_plan_with_repair(...)` re-validates the revised plan via `parse_plan` — **with no knowledge of those frozen ids**. The replanner returns only the unfinished steps (`s2`, …), which still reference `s1`, but omits `s1` from `steps`. So `d not in idset` → `PlanError` → both replan attempts are discarded.

**Why it is purely a validation-time bug:** the executor would run the revised plan correctly.
- `_next_ready` (`engine/tools/planned.py:251`) already treats a dependency as satisfied when `results[d].status == "success"`.
- `_resolve_refs` substitutes `{{s1}}` from the retained `summaries`.
Only `parse_plan` blocks it.

**Impact:** replan-on-failure is effectively disabled for any chained plan (the common shape). Runs still degrade gracefully (honest partial answer, no fabrication) but never get their intended second attempt.

**Relationship to the earlier F3 fix (important):** F3 fixed the *silence* — it added the `replan_failed` trace event and the one-shot repair retry. That visibility is exactly what **surfaced** BUG-1. F3 is done; BUG-1 was the underlying reason the replan failed (now **FIXED & verified**). They are two different things.

**Fix (deterministic, minimal) — allow already-succeeded ids as valid dependency targets during replan validation:**
```python
def parse_plan(text, allowed_tools, max_plan_steps, known_ids=frozenset()):
    ...
    idset = set(ids)
    valid_deps = idset | known_ids          # frozen successes from a prior pass are valid targets
    for s in steps:
        ...
        for d in s.dependencies:
            if d not in valid_deps:          # was: if d not in idset
                raise PlanError(f"step {s.id} depends on unknown step {d!r}")
```
- Thread `known_ids` through `_plan_with_repair` / the planner call.
- On replan pass, pass `known_ids=set(results)`; for the initial plan pass `frozenset()` (unchanged behavior).
- No executor change needed. `_check_acyclic` is unaffected (it already filters deps to in-plan ids).

**Repro:** scripted plan `s1`→`s2`; force `s2` empty on pass 1; the replanner returns `s2` with `dependencies:["s1"]` and omits `s1`. Today: `replan_failed … depends on unknown step 's1'`. After fix: revised plan validates, executes, and reuses `s1`'s frozen result.

**Owner:** Engine.

---

## ISSUE-2 — "Specific open POs / planned orders" is unanswerable; root cause is the semantic view's grain — **Data issue — OUT OF SCOPE for this project (flagged for the data owner)**

> **Scope:** per project direction, semantic-view / data-model gaps are **out of scope** for the agent framework — recorded here as a **data issue** for the view owner. No framework change is expected; the agent's behavior is already correct (grounded, no fabrication). Evidence retained below.

**Symptom:** the open-PO step and the planned-order step both returned zero rows; the agent correctly refused to name transaction IDs and returned a grounded partial answer (derived the target material list, flagged the coverage gap, no fabrication).

**Root cause — confirmed by `DESCRIBE SEMANTIC VIEW OPERATIONS_DEV.OPERATIONS_SANDBOX.INVENTORY_ANALYTICS` (886 rows):** the view has **no transaction grain.**
- **Two logical tables only:**
  - `INVENTORY` — base `BLV_INVENTORY_SNAPSHOTS`, grain = snapshot date × material × plant × storage location.
  - `MATERIAL_MASTER` — base `BV_MaterialMaster`.
  - joined via relationship `INVENTORY_TO_MATERIAL_MASTER` on `MATERIAL_NUMBER_SOURCE` → `MM_MATERIAL_NUMBER_SOURCE`.
- **Supply is exposed only as aggregated snapshot facts:** `TotalSupplyQtyQtrEnd`, `TotalCombinedConsumptionQty13Wk`, `AvgWeeklyConsumptionQty`, `WeeksOnHand*`, composition quantities, and `AVG_PLANNED_DELIVERY_TIME` (= `AVG("PlannedDeliveryTimeDays")`, an average lead time in days — **not** an order).
- **Absent entirely:** any `MRP_STOCK_TRANSACTIONS` table, MRP element / element category (PO vs planned order), order number, order line, planned/delivery date, per-transaction qty/value. (Targeted search returned no matches.)

**Therefore:** a query for "which specific open POs / planned orders to cancel" has nothing to bind to → empty, exactly as observed. **This is not fixable by pack wiring alone** — adding a supply skill/step to the pack cannot help, because the bound view lacks the data.

**Corollary:** the native skill `usecases/inventory_balance/skills/supply_transaction_cancellation.md` references `MRP_STOCK_TRANSACTIONS` / `MRPElementCategory`, which are **not** part of `INVENTORY_ANALYTICS`. So even `inventory_balance` (which owns that skill) cannot retrieve transaction-level supply through this view; the skill currently describes a capability the data binding does not support.

**Fix options (require data-model work, not just prompts):**
1. **Enrich the semantic view** — add an MRP stock-transactions logical table at order-line grain (order no/line, `MRPElementCategory`, planned date, qty, value) related to `INVENTORY` on material/plant, then add a supply-transaction step to `disposition_plan.md`.
2. **Repoint a supply step** at a different view/table that already exposes open orders at that grain, if one exists.
3. **Interim (no data change):** have `disposition_plan.md` / `disposition_report.md` state that "Reduce inbound" yields the *materials/plants* to review with planners, not individual PO/planned-order IDs — which is exactly how the agent already behaved (grounded, no fabrication).

**Owner:** Data / semantic-model (with a small pack follow-up once the grain exists).

---

## Summary

| ID | Severity | Type | Root cause | Status |
|----|----------|------|-----------|-----------|
| **BUG-1** | High | Code (`planned.py`) | `parse_plan` validated deps only against the new plan's step ids; frozen succeeded ids weren't allowed on replan | **FIXED & VERIFIED (2026-09-15)** — `known_ids` in `parse_plan`; 334 pytest (+3 regression) + live re-run replans cleanly, grounded score 15 |
| **ISSUE-2** | — | Data issue (out of scope) | `INVENTORY_ANALYTICS` = snapshot + aggregated supply only; no order-line transaction grain | Flagged for the data/semantic-model owner; **no framework action**. Agent already behaves correctly (grounded, no fabrication). |

---

## Already fixed (verified live, uncommitted on `9293118`) — for completeness, no action needed

| # | Finding | Fix |
|---|---------|-----|
| F1 | `{{sN}}` list ref silently truncated at 600 chars (dropped tail of material list) | `_SUMMARY_MAX` 600→2000 + configurable `max_ref_chars` (default 2000, min 200, cap 8000) + visible truncation marker |
| F3 | Replan failed silently (no trace event, no repair retry) | `_plan_with_repair` (one-shot repair for initial plan and each replan) + `replan_failed` trace event + bounded attempt counting |
| F4 | Planner nested `dependencies` inside step `input` → dispatch rejected it | `_clean_input` strips reserved keys `{id,intent,tool,dependencies}` before dispatch |
| F5 | `'Instruments'` (plural) exclusion label echoed into prose | singular `'Instrument'` in `excess_stock_disposition.md` and `disposition_plan.md` |
| — | s2 forward-demand step intermittently returned EMPTY for the whole material set | `disposition_plan.md` — s2 asks ONE measure + carries a compact comma-separated material-ID list via `{{s1}}`; cross-plant split into s3. `plan.md` — "one concept per step" + "`input` holds only the tool's own fields" |

Post-fix verification: both inventory positives → **18/18** with full material coverage (0 unclassified, empty-steps=0); negatives escalate `no_data` with visible bounded replans. Offline suite: **334 pytest pass** (+3 BUG-1 regression tests in `tests/test_planned.py`).

## By-design (not bugs)

- **opus-5 synthesis latency** on broad (all-plant) inputs (>15 min); scoped (plant + top-N) completes ~70s. Inherent to the synthesis model on large inputs; mitigated by steering the planner to a scoped population.
- **`ops_rca` (mock tools) cannot escalate `no_data`** — mock tools always return canned data, so it declines out-of-scope in prose instead. Real-SQL packs cover the `no_data` path.
- **No cross-turn memory** — the framework is stateless (fresh `initial_state` per turn; `MEMORY_STORE=none` default). A phrase like "the 8 'Reduce Replenishment' items" from a prior turn cannot be resolved; the agent re-derives it. By design.

---

## What works (validated live)

Validated-DAG planning, `{{sN}}` chaining between steps, the dispatch trust boundary (rejected a malformed step input), grounding/escalation on real SQL, honest partial answers, and scoring — all correct. `ops_rca` positive 2-step 18/18; both inventory positives 18/18 after fixes; negatives escalate `no_data` correctly. Single-query packs (`goa_spend`, `procurement_contracts`, `icertis_procurement`, `inventory_balance`) handle multi-*part* aggregation in one SQL but cannot chain step→step — which is what planned mode adds.
