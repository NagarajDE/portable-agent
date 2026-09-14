# Test campaign — what actually works vs. what was only theoretical

Three rounds, newest first. Environment for all: Windows, Python 3, local `.env` pointed at Snowflake
account `illumina`. Memory (thread/session) is not built yet and is out of scope by design.

---

# Round 3 — closing the G1–G4 holes

Date: 2026-09-13 · Under test: **uncommitted working tree on `main`** (parent `f308514`)
Question being answered: *can an irrelevant or blank-returning question still earn a good score?*
→ **No.** 5/5 irrelevant and 3/4 blank-returning live questions now escalate with **no score at all**.

## 3.1 What changed

| # | Fix | Where |
|---|---|---|
| **G1** | The rewrite must ask the USER's question. Prompt may REFUSE (`NO_REFORMULATION`); engine checks deterministically that a distinctive word survived **and** no pinned identifier (`ZZ999`, `PO12345`) was dropped. Refusal/drift keeps the blank, escalates, and does **not** burn the retry budget. | `shared/prompts/reformulate.md`, `engine/graph.py` (`_keeps_subject`, `_pinned_codes`) |
| **G2** | Deterministic **tools** path now marks a blank retrieval (no tool produced usable output → `no_data`, notes carried as the hint) and gets the same reformulate-retry as SQL. `tools: []` (deliberately toolless) is exempt. | `engine/tools/orchestrator.py`, `engine/graph.py` (`_retrieve`, `can_retry`) |
| **G3** | `zero_is_no_data` (**opt-in, default false**): treat one all-zero/NULL row as blank. Off by default because it is indistinguishable from a genuine zero. | `engine/sql_tool.py` (`looks_all_zero`), `shared/config.yaml` |
| **G4** | `data_retries` exposed on `/invoke` and Databricks `custom_outputs`. | both platform agents |

**Deliberately reverted mid-round:** I first made the *agentic* path mark a blank gather too. That broke
`incident_triage` (11/11 → 10/11 mock, 16/16 → 14/16 evals): with `WORKER_PROVIDER=mock` the worker can't
emit tool-call JSON, so the ReAct loop gathers nothing and every creds-free agentic run escalated. That
is the mock's limitation, not the product's — reverted and logged as **G5** below.

## 3.2 Offline verification

| Phase | Result |
|---|---|
| Unit suite | **286 passed** (278 → +8 new grounding tests) |
| Blank-data contract (A–E, 12 assertions) | **PASS** — incl. `judge_calls == 0`, `best_score == -1` |
| Rewrite guards (F1–F4) | **PASS** — refusal escalates without re-querying; **subject drift discarded**; pinned code dropped → discarded; legitimate narrowing still accepted |
| Tools path (G1–G2 checks) | **PASS** — blank tools retrieval retries then escalates; toolless pack **not** escalated |
| Zero-row (H1–H2) | **PASS** — flag off → scored, flag on → escalated; `looks_all_zero` stays narrow |
| Mock sweep, 11 packs | **11/11** grounded + scored |
| Golden evals | **16/16** |
| `/invoke` normal vs. no-data | **PASS** — `score=None, status=no_data, grounded=False, data_retries=1` |
| MCP end-to-end | **PASS** |

## 3.3 Live Cortex — 13 questions, positive + two classes of negative

`CORTEX_SEMANTIC_VIEW` unset (packs self-declared); one fresh process per question.

| Pack | Case | status | score | retries | What the guard did |
|---|---|---|---|---|---|
| `inventory_balance` | POS — on-hand by location | ok | 17/18 | 0 | — |
| `inventory_balance` | NEG employee salary by dept | **no_data** | **none** | 1 | **REFUSED** |
| `inventory_balance` | NEG employee performance ratings | **no_data** | **none** | 1 | **REFUSED** |
| `inventory_balance` | NEG blank — plant `ZZ999` | ok → **no_data** | 16/18 → **none** | 0 | none at first — query returned a NULL **row**; fixed in §3.5 |
| `goa_spend` | POS — top 10 vendors + % | ok | 17/18 | 0 | — |
| `goa_spend` | NEG patents filed last quarter | **no_data** | **none** | 1 | **REFUSED** |
| `goa_spend` | NEG blank — vendor `ACME999XYZ` | **no_data** | **none** | 1 | **DRIFTED** (rewrite dropped the pinned code) |
| `icertis_procurement` | POS — top vendors by SOW value | ok | 16/18 | 0 | — |
| `icertis_procurement` | NEG share price / market cap | **no_data** | **none** | 1 | **REFUSED** |
| `icertis_procurement` | NEG blank — supplier `QQQ404ZZ` | ok → **no_data** | 16/18 → **none** | 1 | non-deterministic: rewrite kept the code, re-query returned rows once and zero rows on re-test (§3.5) |
| `procurement_contracts` | POS — active value by supplier | **no_data** | **none** | 1 | rewrite accepted, re-query **still blank** → genuine gap (F2) |
| `procurement_contracts` | NEG employee performance reviews | **no_data** | **none** | 1 | **REFUSED** |
| `procurement_contracts` | NEG blank — supplier `NOPE777XYZ` | **no_data** | **none** | 1 | rewrite accepted, re-query still blank |

Before/after on the same questions:

| Class | Round 2 | Round 3 |
|---|---|---|
| Irrelevant (employee / patents / share price) | **3/3 scored 16/18, `status=ok`** | **5/5 escalated, no score** |
| Blank-returning (nonexistent entity) | not tested | 3/4 escalated; 1 returned a real NULL row |
| `procurement_contracts` headline question | scored 16/18 (Analyst variance) | escalated as the genuine gap it is |
| Positives | 17 / 18 / 16 | 17 / 17 / 16 — unchanged |

Sample escalated answer (`inventory_balance`, employee salary): *"That question can't be answered from
this data — the INVENTORY_ANALYTICS semantic view…"* — returned with `score=None`, `grounded=false`.

## 3.5 Follow-up — the human must be told WHICH problem it was

Reviewing the two `ok / 16-18` rows above showed the answers were already honest, but the *machine-readable*
surface said success. Both are now fixed:

**(a) One escalation status was not enough.** `status` now carries the reason, and both are unscored:

| `status` | Meaning for a human | Set when |
|---|---|---|
| `ok` | Answered from data | grounded |
| `no_data` | The query ran and matched **nothing** — check the code / filters / freshness | still blank after retry, **or** the rewrite dropped a pinned identifier |
| `out_of_scope` | This data **cannot** answer that question — ask a different agent | the rewrite refused (`NO_REFORMULATION`) or substituted the subject |

The two drift causes are checked separately (`_keeps_subject` vs `_keeps_pinned`) precisely because
"plant `ZZ999`" is *in* scope — only that code matched nothing — so it must report `no_data`, not
`out_of_scope`. Live confirmation of the split:

| Question | status | score |
|---|---|---|
| on-hand balance for plant `ZZ999` | **no_data** | none |
| active contracts for supplier `QQQ404ZZ` | **no_data** | none |
| average employee salary by department | **out_of_scope** | none |

**(b) `zero_is_no_data: true` enabled on the four AI+BI packs.** `ZZ999` returned
`TOTAL_STOCK_QUANTITY | TOTAL_INVENTORY_VALUE / None | None` — one row of NULLs, so the sentinel could
not see it and the run scored 16/18. For these packs a lone all-zero/NULL row is always a filter that
matched nothing, never a real balance.

**Did enabling it regress the happy path?** The flag can only change a run where `looks_all_zero` is
true — **exactly one** data row, every cell `0`/`NULL`/blank. That is the load-bearing argument, and it
is verifiable from the captured evidence: every positive run returned many populated rows (53 plants /
10 vendors / 10 suppliers), so the flag never engaged. Supporting evidence:

| Pack | POS question | Before flag | After flag | Like-for-like? |
|---|---|---|---|---|
| `inventory_balance` | on-hand by location | 17/18, `ok`, 0 retries | 17/18, `ok`, 0 retries | ✅ identical |
| `goa_spend` | top 10 vendors + % | 17/18, `ok`, 0 retries | 17/18, `ok`, 0 retries | ✅ identical |
| `procurement_contracts` | active value by supplier | **escalated** `no_data` | **15/18**, `ok`, 1 retry | ❌ **not comparable** |

The third row is **not** evidence either way: the outcome differs because Cortex Analyst produced no SQL
on one run and usable SQL (after a reformulation) on the other — the same F2 non-determinism seen all
campaign. Worth noting separately that 15/18 is exactly `pass_score`, i.e. a **bare** pass, not a
comfortable one. Plus `test_zero_row_escalates_only_when_pack_opts_in` (off → scored, on → escalated),
11/11 mock packs and 16/16 golden evals with the flag set on all four packs.

The answers were already appropriate throughout; verbatim, after the fix:

> *"No rows returned for plant ZZ999 in INVENTORY_ANALYTICS, so there is no on-hand balance to report —
> the plant code either doesn't exist in the data or has no inventory position on the current snapshot."*

> *"That question can't be answered from this data — the INVENTORY_ANALYTICS semantic model covers
> materials, plants, stock quantities and inventory values only, with no employee, salary, or department
> payroll fields."*

Final counts after the follow-up: **289 unit tests**, 11/11 mock packs, 16/16 golden evals, both
escalation reasons verified over HTTP with `score=None`.

## 3.6 Remaining findings

### G3 boundary — closed for the AI+BI packs · **was P2**
`inventory_balance` "on-hand balance for plant ZZ999" produced a single **NULL row** and scored 16/18.
`zero_is_no_data: true` is now set on all four AI+BI packs, so it escalates as `no_data`. It stays OFF by
default for other packs, where a genuine 0 is a legitimate answer.

### G5 — the agentic path still doesn't mark a blank gather · **P2, open**
An agentic run whose tools all fail still reports `grounded=True` and gets scored. The fix is blocked on
`MockClient` not speaking the ReAct JSON protocol — marking it would break every creds-free agentic run
(demonstrated in 3.1). **Fix:** teach `MockClient` to emit one `{"tool": …}` then `{"final": true}` for an
`act.md` prompt, then apply the same `no_data` marking in `engine/tools/agentic.py`.

### G6 — a bare-number filter can still be dropped · **P3**
`_pinned_codes` protects identifiers that mix letters and digits, deliberately not bare numbers ("top 10"
is a rank, not a filter). So "contracts signed in 1901" may be rewritten to "contracts by year" and score.
Narrower than G1 and, unlike G1, the answer stays on-subject. (A period code like `FY26` *is* pinned — see
§3.7 finding 1 for the re-expression tradeoff.)

### Round-1/2 findings still open
F4 (SDK error messages), F6 (`shared/instructions/`), F7 (pack descriptions), F8 (`run_local` mock escape
hatch), F9 (merge-semantics doc drift), F10 (`LITELLM_MODEL` redundancy). **F2 re-graded again:** the view
still defines no relationships and this run confirmed it live — the pack now *escalates* instead of
scoring, so the bug is contained even though the data gap is unfixed.

---

## 3.7 Code review follow-up (independent reviewer)

An independent reviewer's verdict was **ship it, with one conscious tradeoff**. All three findings addressed:

| Finding | Severity | Resolution |
|---|---|---|
| **1.** `_pinned_codes` false-escalates a re-expressed period code (`FY26`→`fiscal 2026`) | low–med, safe direction | **Accepted + documented** (reviewer's recommended option a). Exempting period codes would let the period be *dropped entirely* and pass — the widening false-pass G1 just closed. The docstring now states this as a known limitation; a test pins the behavior. |
| **2a.** Subject swap with a shared dimension invisible; ≤3-letter nouns invisible | low, was unsafe direction | **Partially closed.** Content-word floor lowered 4→3, so `tax`/`fee` are now visible and a swap with **no** shared dimension is caught (`total tax`→`total fees`). Residual (swap that *keeps* its dimension) is fundamental to a bag-of-words check — documented, with the `NO_REFORMULATION` refusal as the second net. |
| **2b.** `-y`/`-ies` plurals didn't fold (`salary`↔`salaries`) → blocked valid recovery | low, safe direction | **Closed.** `_stem` now folds `-ies`→`-y` and regular `-s`; `salary`↔`salaries`, `category`↔`categories` are no longer read as drift. |
| **3a.** `_keeps_subject` evaluated twice in the drift branch | cosmetic | **Closed.** Hoisted to `kept_subject`/`kept_pinned` locals. |
| **3b.** Toolless path and `gather_context` both returned `"No tool observations."` (grounded vs blank) | cosmetic | **Closed.** Toolless path now returns `"No tools configured; answered from skills alone."`, distinct from the blank-retrieval hint. |

Coverage after the follow-up: **289 unit tests** (new asserts for the tax/fees swap, the `salary`↔`salaries`
fold, and the documented period-code escalation), 11/11 mock packs, 16/16 golden evals.

---

# Round 2 — the blank-data grounding fix

Date: 2026-09-13 · Under test: **uncommitted working tree on `main`** (parent `f308514`)
Question being answered: *can a blank retrieval still earn a good score?* → **No, on the SQL path.**

## 2.1 Summary

| Phase | Result |
|---|---|
| Unit suite (`pytest`) | **278 passed** (was 271; +7 from `test_grounding.py` / `test_env_boundary.py`) |
| Blank-data contract (20 assertions, creds-free, counting judge) | **20/20 PASS** |
| Mock sweep — 11 packs build + answer `sample_task` | **11/11** grounded + scored, no regression |
| Golden-set evals (`run_evals.py`) | **16/16** cases across 11 packs |
| `/invoke` contract — normal vs. no-data | **PASS** — a client cannot read a pass on no-data |
| MCP end-to-end vs. real stdio server | **PASS** (regression) |
| Live Cortex, 9 questions across 4 packs (positive + negative) | 8 grounded/scored, **1 escalated `no_data` live** |

**Verdict on the fix: it does what it claims.** Groundedness is decided deterministically at the tool
boundary, the judge is never given the chance to score a blank, and every surface reports it distinctly.
Three adjacent holes remain (G1–G3); G1 is the one that matters and the retry mechanism *causes* it.

## 2.2 Blank-data contract — 20/20

Run creds-free with stubbed SQL + a **counting** judge, so "the judge never ran" is measured, not assumed.

| # | Assertion | Result |
|---|---|---|
| A1–A4 | `is_no_data(no_data())`; hint round-trips; **zero rows → sentinel**; real rows → not sentinel | PASS |
| B1 | `grounded == False` after retries exhausted | PASS |
| B2 | `status == "no_data"` | PASS |
| B3 | `best_score` stays **-1** — no score is ever assigned | PASS |
| B4 | **judge NEVER called** (`judge_calls == 0`) | PASS |
| B5 | retried once, then escalated (2 SQL calls total) | PASS |
| B6 | honest answer preserved for the human | PASS |
| B7 | worker prompt never contains the raw `__PA_NO_DATA__` sentinel | PASS |
| B8 | worker prompt *does* carry the tool's hint | PASS |
| C1–C4 | retry **recovers** → `grounded=True`, judge runs, scores 18, second query used the reformulation | PASS |
| D1–D2 | `max_data_retries=0` → one query, no retry, immediate escalation, judge silent | PASS |
| E | `max_data_retries` of `6` and `-1` both rejected at build time | PASS |

`/invoke` surface: normal → `score=16, status=ok, grounded=True`; blank → `score=None, status=no_data,
grounded=False`, HTTP 200 with the honest message. `run_local.py` prints `RESULT: NO USABLE DATA
(escalated — retried Nx, still blank)` instead of a score.

## 2.3 Live Cortex — positive and negative, `CORTEX_SEMANTIC_VIEW` unset (packs self-declared)

One fresh process per question (`snowpark_session()` is memoized per process).

| Pack | Case | Question | status | grounded | score | retries |
|---|---|---|---|---|---|---|
| `inventory_balance` | POS | total on-hand balance by location | ok | yes | 17/18 | 0 |
| `inventory_balance` | **NEG** | average employee salary by department | ok | yes | **16/18** | 1 |
| `goa_spend` | POS | top 10 vendors by spend + % of total | ok | yes | **18/18** | 0 |
| `goa_spend` | **NEG** | patents filed last quarter | ok | yes | **16/18** | 1 |
| `icertis_procurement` | POS | top vendors by active SOW value | ok | yes | 16/18 | 0 |
| `icertis_procurement` | **NEG** | current share price and market cap | ok | yes | **16/18** | 1 |
| `procurement_contracts` | POS (old F2 case) | active contract value by supplier, top 10 | ok | yes | 16/18 | 0 |
| `procurement_contracts` | POS | count of active contracts | ok | yes | 16/18 | 0 |
| `procurement_contracts` | **NEG** | employee performance review scores | **no_data** | **no** | **N/A** | 1 |

Grounded answers were specific and checkable — `$708.18M across 53 plants`; `Belastingdienst / Douane
$181.9M (3.67%) of $4,958.5M`; `Techcov Pte Ltd $78,000,000`; `364 active contracts (256 MSAs
$534,521,700 + 108 SOWs $132,633,304)`. **No fabrication in any of the 9 runs**, including all negatives.

Two live outcomes worth recording:

- **The escalation fires for real.** `procurement_contracts` NEG stayed blank through the retry →
  `status=no_data`, `grounded=False`, no score, judge never invoked: *"Employee performance review scores
  aren't available — the semantic view is grained at one row per contract (AGREEMENT_CODE)…"*
- **Round 1's F2 case now returns data — but not because of this fix.** `retries=0` means the retry never
  fired; Cortex Analyst simply produced SQL this time where it previously returned a "no relationships"
  clarification. That view's behavior is **intermittent**, so F2 is re-graded, not closed.

## 2.4 New findings

### G1 — Out-of-scope questions still return `status=ok` and a **passing** score; the retry *causes* it · **P1**

**3 of 4 live negatives** scored 16/18 with `status=ok, grounded=True` — and all three had `retries=1`,
meaning the first retrieval **was** blank and would have escalated correctly. The reformulation rescued
them into a pass: asked to rewrite an unanswerable question, the model drops the original subject and
asks a question the *view* can answer, rows come back, `grounded` flips to True, and the judge scores the
honest "that isn't in this data" answer as a pass. From `inventory_balance` NEG:

> "The DATA contains no employee, salary, or department fields — this is inventory analytics only, so
> average salary by department cannot be answered; **what the evidence actually returns is average
> inventory value per plant** across 53 [plants]"

That is a 16/18. The worker is honest — the defect is entirely in the score/status surface: a client
sees `score=16, status=ok, grounded=True` for a question that was never answered. `grounded` currently
means *"rows came back"*, not *"rows that answer this question came back"*, and `reformulate.md`'s
"keep the user's original intent" instruction is not strong enough to stop the pivot.

**Fix (cheap, fits the existing shape):** let reformulation *refuse*. Instruct `reformulate.md` to reply
exactly `NO_REFORMULATION` when the question's subject isn't covered by the model at all, and treat that
token like an empty reply in `engine/graph.py` (keep the blank marker → escalate). The loop already has
the `data = sql.ask(new_q) if new_q else data` shape; it needs one explicit token check. Without this,
the retry converts correct escalations into passes.

### G2 — The deterministic *tools* path has no blank detection · **P2**

`gather_context(...)` with nothing to report returns the prose `'No tool observations.'`, and
`is_no_data()` on it is **False** → `grounded` stays True → the judge scores it. The dev deliberately
excluded the **agentic** path (it self-corrects across its own tool steps, so that is defensible), but
the deterministic tools path fires **exactly once**, structurally identical to the SQL path, and is
uncovered. Affects `api_assistant` and any future tools pack. **Fix:** return `no_data()` from
`gather_context` when there are no usable observations and drop `use_sql` from the retry-loop condition.

### G3 — Zero-*valued* aggregates are invisible to the sentinel · **P2**

Live: `procurement_contracts` "How many active contracts are there in total?" → `ACTIVE_CONTRACT_CNT = 0`.
That is **one row**, so `is_no_data()` is False, `grounded=True`, score 16/18 — same user-visible failure
mode as a blank, different mechanism. The sentinel keys on row **count**, but `COUNT(*) = 0` and
`SUM(...) = NULL` always return a row. Not a defect in the fix (rows genuinely came back), and the worker
flagged it well (*"worth verifying the BUSINESS_STATUS values actually present"*). **Fix:** document the
boundary, or add an opt-in heuristic for a single row whose measures are all `0`/`NULL`.

### G4 — `/invoke` does not expose `data_retries` · **P3**

`run_local.py` prints it; `AskResponse` omits it, so an operator can't distinguish a first-try answer
from a reformulated one — exactly the signal G1 makes interesting. One field on the model.

## 2.5 Round-1 findings closed by this change

| Finding | Status |
|---|---|
| **F3** — no grounded signal on zero rows | **Closed** — this is the fix; deterministic, not rubric-based |
| **F1** — `mcp` imported but absent from `requirements.txt` | **Closed** — added with an accurate "optional" note |
| **F5** — `tests/conftest.py` env isolation incomplete | **Closed** — enumerated and grouped, now includes creds and semantic-layer vars |
| **F2** — `procurement_contracts` can't answer its `sample_task` | **Re-graded → P2, intermittent.** Got data this run (`retries=0`); the retry covers it when it recurs, but the view still defines no relationships |
| F4, F6–F10 | Unchanged (see Round 1) |

---

# Round 1 — feature bundles

Date: 2026-09-13 · Commit under test: `f308514` ("fixing scoring error")

Purpose: after landing four feature bundles (instructions, tools/agentic, MCP, AI gateway) plus the
per-pack semantic layer, exercise **everything testable** and separate proven behavior from
aspirational behavior.

## 1. Summary

| Phase | Result |
|---|---|
| Unit suite (`pytest`) | **271 passed**, stable across repeat runs and `--collect-only` |
| Mock sweep — all 11 packs build + answer `sample_task` | **11/11 OK** (16/18, 2 iterations each) |
| Golden-set evals on mock (real `run_evals.py` path) | **16/16 cases pass** across 11 packs |
| `pack_manifest` / sample questions | **11/11** expose non-empty samples (golden-set fallback works) |
| Instructions (composition, zero-injection, runtime gate, `strict_bool`) | **All pass** |
| Semantic-layer guard rails | **All pass** (self-declare, missing-layer, mock-ignores, genie) |
| **MCP end-to-end vs. a real stdio server** | **WORKS** (was assumed untestable) |
| **Agentic ReAct loop live with a real model** | **WORKS** — model-chosen tool order, 18/18 |
| Live Cortex AI+BI packs (4, fresh process each) | 3 fully grounded; 1 retrieved **no data** (finding F2) |
| AI gateway (LiteLLM) live | **Not testable** — `litellm` not installed, no keys |
| FastAPI shell (`/healthz`, `/manifest`, `/invoke`) | **All pass**, incl. 422 validation |

### Live Cortex results (env `CORTEX_SEMANTIC_VIEW` **removed** — packs self-declared)

| Pack | Semantic view (from `semantic_layer.yaml`) | Score | Grounded? |
|---|---|---|---|
| `inventory_balance` | `OPERATIONS_DEV.OPERATIONS_SANDBOX.INVENTORY_ANALYTICS` | 16/18 | yes — $708.18M / 54 plants |
| `goa_spend` | `DB_ENTERPRISE_DW_VAL.GSC_PROCUREMENT.INDIRECT_SPEND` | **18/18** | yes — $4,958.5M grand total |
| `icertis_procurement` | `OPERATIONS_DEV.OPERATIONS_SANDBOX.ICERTIS_PROCUREMENT_ANALYTICS` | 16/18 | yes — flagged $34.9M blank-party gap |
| `procurement_contracts` | `DB_ENTERPRISE_DW_VAL.GSC_PROCUREMENT.ICERTIS_CONTRACTS` | 15/18 | **NO — zero rows (F2)** |

Two different databases across the set, each in its own process — per-pack semantic-layer
declaration is confirmed working live.

## 2. Proven working (not theoretical)

- **MCP adapter, end to end.** Against a real local FastMCP **stdio** server: live call returned
  `pong: hello-from-portable-agent`; a raising tool became `ok=False, kind=runtime` (the `isError`
  fix); `read_only` defaults **False** (fail-closed); a missing `command` is rejected at construction.
- **Agentic ReAct loop, live.** A real Cortex worker emitted valid JSON tool calls and **all three
  tools fired in an order the model chose** (`health` → `catalog` → `deploys`, not config order),
  synthesizing: *"orders-api (tier 1, owned by team-fulfillment) is degraded with 1 open incident
  right after its v2.4.1 deploy 12m ago"* — **18/18**.
- **Instructions.** Pack with no `instructions/` → **zero injection** in generate *and* rubric; pack
  with instructions → block in both. Runtime gate **off by default** (instruction not leaked), honored
  when on. `strict_bool` rejects `'maybe'`, and a quoted `'false'` **does not fail open**.
- **Semantic layer.** 4 packs self-declared with env unset; non-AI+BI packs (`dq_qals`,
  `api_assistant`, `incident_triage`) guard-railed with actionable messages; `SQL_TOOL=mock` ignores a
  declared layer; `genie` on a snowflake-only pack is guard-railed.
- **Gateway resolution rules** (no provider SDK needed): pack `models:` honored; **M6** — an env
  provider override correctly *drops* the pack model (`worker=cortex:auto`, no Anthropic model sent to
  Cortex); **NB1** provider case-insensitivity; **M8** same-provider judge inherits worker model;
  **M7** `provider: auto` normalized instead of `KeyError`.
- **Anti-fabrication.** When Cortex Analyst returned no SQL, the worker did **not** invent numbers — it
  reported the failure and its cause. Worth keeping as a regression expectation.

## 3. Findings

### F1 — `mcp` is imported but NOT declared in `requirements.txt` · **P1** → closed in Round 2
`engine/tools/mcp_tool.py` imports `mcp`; `requirements.txt` never lists it. It works here only
because `mcp 1.27.1` happens to be installed from elsewhere. A clean
`pip install -r requirements.txt` produces an MCP tool that constructs fine and fails at first call.
**Fix:** add `mcp` to requirements (optional-extra section is fine).

### F2 — `procurement_contracts` cannot answer its own `sample_task` · **P1** → re-graded P2 (intermittent)
Live, Cortex Analyst returned **no SQL** — only text:
> "I think multiple tables are needed… the provided semantic model does not define any relationships
> between the tables. Please add the appropriate relationships and try again."

So the pack retrieves **zero rows** for its headline question yet still scores 15/18 (= `pass_score`).
The prior session's 15/18 for this pack was likely the same root cause, not answer quality.
**Fix (pick one):** define the relationships in the native semantic view, or rescope the pack's
`sample_task`/skills to the single `ICERTIS_CONTRACTS` table (the model itself suggested
`BUSINESS_STATUS = 'Active'`).

### F3 — No "grounded" signal when the SQL tool returns no rows · **P2** → closed in Round 2
A run that retrieved no data passed at exactly `pass_score`. The answer was honest, so the score isn't
*wrong* — but score alone cannot distinguish "answered from data" from "correctly reported it
couldn't." Evals and dashboards therefore can't detect a pack silently degrading to no-data.
**Fix:** set a `data_ok`/`grounded` flag in state + emit it in tracing (detectable: Analyst returned
text, not rows), and/or add a rubric axis that penalizes absent evidence.

### F4 — Missing provider SDKs raise raw `ModuleNotFoundError` · **P2**
`litellm` → `ModuleNotFoundError: No module named 'litellm'`; same for `anthropic` / `openai`. No
"pip install" guidance, unlike `GenieTool`'s helpful message. Compounding: `litellm` is the
*recommended* real provider and is declared in requirements but **not installed**, so the entire AI
gateway path (routing, fallback, proxy/`LITELLM_BASE_URL`) is **unverified**.
**Fix:** wrap the lazy imports with an actionable error; install `litellm` and smoke-test one call.

### F5 — `tests/conftest.py` env isolation is incomplete · **P2** → closed in Round 2
`_APP_ENV` omits `CORTEX_SEMANTIC_VIEW`, `CORTEX_SEMANTIC_MODEL`, `SNOWFLAKE_*`, `ANTHROPIC_API_KEY`,
`CORTEX_MODEL`, `CORTEX_ANALYST_TIMEOUT_SECONDS`. A developer with those exported can get different
test behavior than CI. **Fix:** add them to the isolation list.

### F6 — `shared/instructions/` does not exist · **P2**
The design specified `shared/instructions/house_style.md`. Consequences: `exclude_shared_instructions`
is currently a **no-op key** (nothing shared to exclude), and the advertised "shared + pack"
instruction composition is only exercised on the pack side. **Fix:** ship a shared default, or
document that the shared tier is intentionally empty and mark the key as forward-looking.

### F7 — 9 of 11 packs have no `description` · **P3**
`pack_manifest` (and `/manifest`) expose `description`; only `incident_triage` and `_TEMPLATE` set it,
so a UI shows blanks for the rest. **Fix:** backfill one line per pack.

### F8 — `run_local.py` can't be forced to mock from the shell · **P3**
It calls `load_dotenv(override=True)`, so a `.env` configured for Cortex beats exported env vars; a
creds-free mock run requires editing `.env`. This campaign had to bypass `run_local.py` for the mock
sweep. By design, but it costs testability. **Fix (optional):** honor an explicit
`PORTABLE_AGENT_NO_DOTENV=1` escape hatch in the runner.

### F9 — Doc drift on config merge semantics · **P3**
`models:` is now deep-merged per role, but the merge table in
`docs/concepts/use-case-pack-anatomy.md` still says maps are shallow-merged and lists replaced.

### F10 — `LITELLM_MODEL` is a third redundant model source · **P3**
`LiteLLMClient` falls back to it even though `_resolve_models` already resolved
`WORKER_MODEL`/`EVAL_MODEL`. Harmless but widens the precedence surface.

## 4. Not testable here (and why)

| Surface | Why | To test later |
|---|---|---|
| LiteLLM live routing / fallback / proxy | `litellm` not installed; no provider keys | `pip install litellm` + one key, or point `LITELLM_BASE_URL` at a proxy |
| Databricks / Genie | Adapter is a stub; no SDK or creds | Wire `GenieTool`, then repeat this campaign |
| MCP over SSE/HTTP (`url`) | Transport not wired (stdio only, fails fast) | Wire the transport, then reuse the probe pattern |
| MCP vs. a third-party server (Jira, etc.) | None integrated | Same harness, real `command` |
| Memory (thread/session) | Not built | After implementation |
| `allow_runtime_instructions` under real endpoint RBAC | Needs a deployed, access-controlled endpoint | SPCS deploy + RBAC test |

## 5. Method (reproducible)

All creds-free phases ran in-process with `WORKER_PROVIDER/EVAL_PROVIDER/SQL_TOOL=mock` set **before**
importing `engine`, and without `load_dotenv`, so `.env` could not override the mock. Live phases
loaded `.env` for credentials only and **popped** `CORTEX_SEMANTIC_VIEW`/`CORTEX_SEMANTIC_MODEL` to
prove per-pack self-declaration. Each live pack ran in a **separate process** because
`snowpark_session()` is memoized per process. The MCP probe wrote a temporary FastMCP stdio server to
a temp dir and removed it afterward. No test scaffolding was added to `engine/` or committed.
