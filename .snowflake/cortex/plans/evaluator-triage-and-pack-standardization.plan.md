# Evaluator triage (round 2) + pack standardization

## Context

Second-pass review (Astra) agrees with 7 of my challenges, partials on 2, disagrees on 0 - confirming "keep the layout, document it." The two items it presses (#3 exemplar format, #7 skill auto-concat) are **elevated to do-now**, because both conditions already exist in the repo:
- Two exemplar formats already ship (`sql` pairs; `api_assistant` uses `input`/`output`), dispatched by implicit key-sniffing in `_format_exemplar` ([engine/graph.py](engine/graph.py) L67-75).
- `shared/skills/sql_safety.md` is already injected into the non-SQL `api_assistant` pack (`load_skills` concatenates all shared skills, L58-64).

All other verdicts from the prior plan stand (verified against source): apply NB3/NB4/NB5, P1/P2/P3, T1-T5, D2; challenge D1 (already documented), P4/P5 (documented/by-design), and the folder renames (`rubrics/`, `system.md`, `semantics/`, `schemas/`, `mocks/`, `extends:`).

## What changed vs. the prior plan
- **#3 exemplar `kind:` -> DO NOW** (was optional): add an explicit discriminator, keep key-sniffing as fallback, document the field schema.
- **#7 shared-skill opt-out -> BUILD NOW** (was "note as future"): config-driven `exclude_shared_skills:`, applied to `api_assistant`.
- Per-pack README backfill remains dropped.

## Triage summary (verdicts)

| Area | Apply | Challenge |
|---|---|---|
| HTTP (http_tool.py) | NB4 `urljoin(resp.url, loc)`; NB5 IPv6 `is_site_local`; NB3 docstring precision | - |
| Inventory pack | P1 reframe skills to evidence-interpretation; P2 snapshot-aware exemplar + honest label; P3 "when present" qualifiers | P4 (documented max_score caveat; add author note); P5 (mock-harness by design; add robustness note) |
| Wiring test | T1 all skills in refine + non-empty; T2 all exemplars; T3 generate-template check; T4 rubric precedence (shared-only line ABSENT); T5 explicit mock guard | - |
| Docs | D2 tighten liveness sentence | D1 (max_stall already documented, refine-loop.md L92-102, L144-146) |
| Structure | inheritance/merge guide + checklist + non-SQL example; exemplar `kind:` (#3); `exclude_shared_skills:` (#7) | rubric/generate/fixtures renames; semantics/; schemas/; inherits->extends |

## Implementation steps

1. **HTTP safety** ([engine/tools/http_tool.py](engine/tools/http_tool.py)): NB4 redirect base -> `urljoin(resp.url, loc)` (preserves query on fragment-only redirects); NB5 add `isinstance(ip, ipaddress.IPv6Address) and ip.is_site_local` to `_ip_is_blocked`; NB3 reword `_read_body` docstring (socket timeout = per-read inactivity, not max duration; dispatch future is the hard bound). Add 2 tests to [tests/test_tools_http_safety.py](tests/test_tools_http_safety.py): fragment redirect keeps query; `fec0::1` blocked.

2. **Inventory correctness**: reframe [usecases/inventory_balance/skills/snapshot_and_metrics.md](usecases/inventory_balance/skills/snapshot_and_metrics.md) from query rules to evidence-interpretation (e.g. "if evidence spans multiple SnapshotDates, do not sum across them - report per-snapshot or state the current snapshot is needed"); add "when present/available" qualifiers to [.../skills/inventory_reporting.md](usecases/inventory_balance/skills/inventory_reporting.md); rewrite [.../exemplars/verified_queries.yaml](usecases/inventory_balance/exemplars/verified_queries.yaml) snapshot-aware with realistic columns + relabel as illustrative seed; add substring-robustness note to [.../evals/golden_set.yaml](usecases/inventory_balance/evals/golden_set.yaml).

3. **Rubric author note** (P4): one comment line atop shipped rubrics (shared + 4 pack overrides) - axes must sum to `{max_score}`; shipped as 3x6=18.

4. **Exemplar format discriminator (#3)** ([engine/graph.py](engine/graph.py) `_format_exemplar`): honor optional `kind: text_to_sql | input_output`; when absent, fall back to current key-sniffing (backward compatible). Add a test for explicit `kind:` in both directions. Document the field schema (required vs optional per kind) in the authoring guide.

5. **Shared-skill opt-out (#7)**: extend `load_skills(use_case, exclude=None)` to skip shared skills whose stem is in the pack config's `exclude_shared_skills:`; `build_graph` passes `cfg.get("exclude_shared_skills")`. Default None = load all (SQL packs unchanged). Set `exclude_shared_skills: [sql_safety]` in [usecases/api_assistant/config.yaml](usecases/api_assistant/config.yaml). Add a test: `api_assistant`'s generate prompt contains the `reporting` fingerprint but NOT the `sql_safety` fingerprint.

6. **Strengthen wiring test** ([tests/test_pack_artifacts_wired.py](tests/test_pack_artifacts_wired.py)): T1 assert all shared+pack skill fingerprints in refine and each non-empty; T2 assert every exemplar reaches generate; T3 assert the effective `generate.md` distinctive line reaches generate; T4 add a negative assertion that a shared-rubric-ONLY line is absent when a pack overrides (proves replacement, not concat); T5 explicit mock/socket guard in the api_assistant case.

7. **Authoring guide** `docs/concepts/use-case-pack-anatomy.md`: standard skeleton (required vs optional); per-file guide; composition/inheritance table with **exact merge semantics** (map merge, scalar replace, list replace for config; whole-file override for prompts; concat-with-opt-out for skills); "override is by file presence, not config"; the "skills guide the worker/judge, not the executed query; query semantics live in the semantic model" boundary; exemplar schema + `kind:`; `exclude_shared_skills:`; a **non-SQL pack example** (api_assistant); a new-pack checklist (incl. "non-SQL packs: exclude sql_safety"). Also tighten the liveness sentence in [docs/concepts/end-to-end-flow.md](docs/concepts/end-to-end-flow.md) (D2).

8. **Scaffold + wiring**: runnable `usecases/_TEMPLATE/` (mock-only; omits rubric/refine to show inherit) + `usecases/README.md` index; add `usecases/api_assistant/__init__.py` if missing; cross-link [README.md](README.md) docs block and the [CLAUDE.md](CLAUDE.md) folder/composition section (note `exclude_shared_skills` + exemplar `kind:`); confirm no test auto-discovers packs (exclude `_`-prefixed if so).

## Verification
- `py -3 -W ignore -m pytest -q` -> all green (existing 180 + new HTTP/exemplar/skill-opt-out tests; strengthened wiring assertions still pass).
- New unit tests pass: NB4 query-preserved-on-fragment-redirect; NB5 `fec0::1` blocked; exemplar `kind:` both directions; `api_assistant` excludes `sql_safety` but keeps `reporting`.
- `python run_local.py _TEMPLATE` on mock runs end-to-end.
- Optional live `inventory_balance` re-run to confirm reframed skills still yield the house-formatted answer with no fabricated descriptions when data lacks them.

## Critical files
- [engine/graph.py](engine/graph.py) - `_format_exemplar` (`kind:`), `load_skills`/`build_graph` (`exclude_shared_skills`).
- [engine/tools/http_tool.py](engine/tools/http_tool.py) - NB3/NB4/NB5.
- [tests/test_pack_artifacts_wired.py](tests/test_pack_artifacts_wired.py) - T1-T5 + the sql_safety-exclusion assertion.
- [usecases/inventory_balance/skills/snapshot_and_metrics.md](usecases/inventory_balance/skills/snapshot_and_metrics.md) - P1/P3 reframe.
- New `docs/concepts/use-case-pack-anatomy.md` + `usecases/_TEMPLATE/` - the authoring standard.

## Out of scope / challenged (no change)
No folder renames; no `semantics/` or `schemas/`; no `inherits:`->`extends:`; no per-pack README backfill; no loop/behavior changes; native `INVENTORY_ANALYSIS_AGENT` untouched. Engine changes are limited to two generic, backward-compatible, config-driven composition features (`kind:`, `exclude_shared_skills`).