---
name: "framing all packs"
created: "2026-09-14T20:43:39.568Z"
status: pending
---

# Plan: Option-2 framing + enable across all 10 packs + full native skill port

## Goal

1. Make query framing **minimal** (Option 2): bind a named value to the right column + name the measure, and **add nothing else** (no scope/date/derived-metric padding).
2. Enable `frame_query` on **all 10 packs** (your choice).
3. **Port the native agents' skills** for the 4 packs that mirror one.

## Honest scope realities (baked into the plan)

- **Native skill port = 4 AI+BI packs only.** Confirmed native agents: `GOA_SPEND_ANALYTICS_AGENT`, `INVENTORY_ANALYSIS_AGENT`, `ICERTIS_PROCUREMENT_AGENT`, `PROCUREMENT_CONTRACT_AGENT`. The other 6 packs (`api_assistant`, `incident_triage`, `dq_qals`, `kpi_analytics`, `anomaly_rca`, `parity_hana_snowflake`) are generic/original — **nothing to port**; they just get `frame_query` on with their existing skills.
- **Framing's proven value is only on the 4 Analyst-backed packs** (semantic view + dimension-value ambiguity). On the other 6 it's an extra LLM call with little upside; we enable per your "all 10" and verify no regression.
- **`incident_triage` is agentic** — the current gate (`use_sql or deterministic`) won't fire framing there. Honoring "all 10" needs a small engine change (Phase 0.3). It cannot be exercised on MOCK (agentic needs a real worker).
- **Bloat risk:** porting \~10 skills into a pack while framing concatenates *all* skills would re-bloat the framing prompt (the over-reach Option 2 prevents). Fixed by a `frame_skills:` subset knob (Phase 0.2), default = all (so `goa_spend` stays byte-identical until we set it).

---

## Phase 0 — Foundation (engine + prompt)

### 0.1 Tighten `shared/prompts/frame.md` (Option 2)

Rewrite the rules to be strict:

- ONLY (a) bind a named value to the dimension/column that holds it, and (b) optionally name the default measure.
- Explicitly FORBID adding a filter, date grain (e.g. "fiscal year"), scope default (e.g. "including NULL rows"), ranking, share-of-total, or any derived metric the user did not ask for.
- If there is no named value to bind, return the question **unchanged**.
- Keep the existing subject/pinned-id preservation and single-line output.

### 0.2 Add `frame_skills:` config knob (keeps framing focused after the port)

- New optional pack config: `frame_skills: [file1.md, file2.md]` — the skill files framing uses. **Default = all loaded skills** (backward compatible; `goa_spend` behavior unchanged until set).
- `engine/graph.py`: build a `frame_skills_text` (subset) at graph-build time; `_frame_question` uses it instead of the full `skills`. Answer synthesis (`generate.md`) and `refine.md` keep using the **full** `skills`.
- Mirrors the existing `exclude_shared_skills` convention (one small, well-understood pattern).

### 0.3 Extend framing to the agentic path (for `incident_triage`)

- Currently framing runs only before the single-shot retrieval. Add framing of the question **once** before the agentic ReAct loop starts, reusing `_frame_question` (same drift guards, same fail-safe to raw).
- Gate stays opt-in via `frame_query`; on MOCK the agentic loop is skipped, so this is inert there (documented).

### 0.4 Unit tests (offline)

- `frame_skills` subsetting: framing sees only the listed skills; answer step still sees all.
- Agentic-path framing: framed question reaches the loop; drift/empty → raw.
- Strict prompt behavior stays covered by the existing 4 framing tests (adjust expectations if the prompt wording changes an assertion).

---

## Phase 1 — Port native skills (4 AI+BI packs)

### 1.1 Introspect each native agent

`DESCRIBE AGENT DBADMIN.AGENTS.<AGENT>` for all 4 → read `tools`, `instructions.response`, and the skills stage path; `LIST @<skills_stage>` to enumerate each agent's `SKILL.md` files.

### 1.2 Read the native `SKILL.md` files

Read each file's content. (The earlier inline-file-format read failed; at implementation time I'll create a **session TEMPORARY file format** to read them cleanly — that's DDL, not allowed in plan mode. Fallback if any file is truly unreadable: author from live-queried values + the agent's instructions, as was done for `category_map.md`.)

### 1.3 Port into each pack's `skills/`

Adapt each native `SKILL.md` into our concatenated-skill format under `usecases/<pack>/skills/`:

- `goa_spend`: add the other \~9 (`vendor_spend_lookup`, `time_comparison`, `top_n_ranking`, `cost_center_gl_lookup`, `regional_currency_filter`, `non_po_concur_spend`, `tail_spend_consolidation`, `contract_renewal`, `compliance_scorecard`) alongside the existing `category_map` + `spend_rules`.
- `inventory_balance`, `icertis_procurement`, `procurement_contracts`: port their native skill sets (count/list determined in 1.1).
- These are **answer-side** parity skills (they improve synthesis; they are not all framing maps).

---

## Phase 2 — Framing subset + enable per pack

### 2.1 Designate the value→column framing subset per AI+BI pack (`frame_skills:`)

- `goa_spend`: `frame_skills: [category_map.md]` (keep framing focused after the +9 port).
- `icertis_procurement` / `procurement_contracts`: their existing skills already name columns/measures; author a small `column_map.md` (which column a named value lives in: `BUSINESS_STATUS` for active/expired, `UPDATED_CONTRACT_VALUE` measure, `SOURCING_CATEGORY`/`GEO_*` dimensions) and point `frame_skills` at it.
- `inventory_balance`: author a small `dimension_map.md` (e.g. `ProductType` ∈ {Consumables, Instruments, Spares, Other}; the money/quantity measures) and **reconcile** `snapshot_and_metrics.md`'s "do NOT rewrite the query" wording so it no longer contradicts an enabled framing step (framing binds values; the snapshot default stays in the semantic model, never injected by framing).

### 2.2 Enable `frame_query: true`

- 4 AI+BI packs (goa\_spend already on) + `frame_skills` set as above.

- 6 generic packs: set `frame_query: true` (honoring "all 10") using existing skills.

  - `api_assistant` (deterministic tools): framing rewrites the task before tool inputs — verify the mock catalog/health still match; if it misfires, scope `frame_skills` to its `answering.md` or reconsider.
  - `incident_triage`: relies on Phase 0.3; document it's a no-op on MOCK.
  - `dq_qals`, `kpi_analytics`, `anomaly_rca`, `parity_hana_snowflake`: framing fires on their SQL path; expected low value, verify no regression.

---

## Phase 3 — Verify (offline + live)

### 3.1 Offline (no creds)

- `pytest -q` (existing 293 + new Phase-0 tests).
- Mock sweep: build+invoke all 10 packs.
- Golden evals: 15/15 across the 10 packs.

### 3.2 Live (Cortex, authorized, per-pack process)

- **`goa_spend` Round-5 set**: confirm rewrites are now **lean** (Q1/Q2 unchanged or measure-only; no scope/share padding) AND the IT Software case still returns **$21,339.34**, negatives still escalate, bogus category still grounded-honest.
- **inventory\_balance / icertis\_procurement / procurement\_contracts**: 1 positive that exercises a value→column binding + 1 negative each; confirm framing binds correctly and no over-reach.
- **Spot-check one non-AI+BI pack** (e.g. `parity_hana_snowflake` or `dq_qals`) to confirm framing didn't regress it.

### 3.3 Docs + memory

- `docs/concepts/end-to-end-flow.md`: note the tighter framing + `frame_skills` + agentic framing.
- `docs/concepts/use-case-pack-anatomy.md`: document `frame_skills`, update the section-5 "opt-in exception", and the inventory reconciliation.
- `docs/testing/test-campaign-results.md`: add **Round 6** (Option-2 framing, all-packs enablement, native port, live results).
- Update project memory `open_findings.md`.

---

## Deliverables / files touched

- **Engine/prompt:** `shared/prompts/frame.md` (tighten), `engine/graph.py` (`frame_skills` subset + agentic-path framing), `tests/test_grounding.py` (new tests).
- **Skills (port):** new `SKILL.md` ports under all 4 AI+BI packs' `skills/`; new small `column_map`/`dimension_map` framing skills; edit `inventory_balance/skills/snapshot_and_metrics.md` wording.
- **Config:** `frame_query: true` on all 10 packs; `frame_skills:` on the AI+BI packs.
- **Docs/memory:** the four files above.

## Risks / decisions to accept

- Framing on the 6 non-Analyst packs adds cost with little proven benefit (accepted per "all 10").
- `incident_triage` framing requires the agentic-gate change and can't be MOCK-tested.
- `api_assistant` framing could disturb mock-tool matching — will verify and scope its `frame_skills` if needed.
- Native `SKILL.md` reading depends on a temp file format at implementation time; fallback is authoring from live values.
- Live runs consume credits (authorized).

## Out of scope

- Porting native **orchestration instructions** verbatim (beyond skills) and any semantic-view changes.
