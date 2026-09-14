# The three "try again" moments — reformulate vs. refine vs. re-review

The loop retries in **three different places**, for **three different reasons**. They're easy to
confuse because all three call an LLM a second time — but each fixes a *different* problem, acts on a
*different* thing, and is bounded by a *different* config knob. This doc pins them down with a
comparison table and a worked question at the end.

> Short version:
> - **Reformulate + retry** fixes the **question** — "we got no data back, let me ask differently."
> - **Refine** fixes the **answer** — "the answer scored low, let me improve it."
> - **Re-review** fixes the **score** — "the judge's reply was garbled, let me get a readable score."

---

## Where each one lives

```
question
   │
   ▼
┌─────────────────────────────────────────── GENERATE ───────────────────────────────────────────┐
│  1. retrieve data (SQL tool / tools)                                                             │
│         │                                                                                        │
│         ├─ got rows ──────────────────────────────────────────────┐                              │
│         │                                                          │                             │
│         └─ BLANK ──►  ①  REFORMULATE + RETRY   (rephrase the       │                             │
│                          QUESTION, re-query)  ×max_data_retries    │                             │
│                             │                                      │                             │
│                             ├─ recovered data ───────────────────►│                              │
│                             ├─ REFUSED / off-subject ──► ESCALATE (out_of_scope, judge skipped)  │
│                             └─ still blank ──► ESCALATE (no_data, skip the judge, hand to human) │
│                                                                    ▼                             │
│  2. worker writes the answer (rev0) from the data                                                │
└──────────────────────────────────────────────────┬──────────────────────────────────────────────┘
                                                    ▼
┌───────────────────────────────────────── EVALUATE (the judge) ──────────────────────────────────┐
│  parse the judge's reply into SCORE: N/max - reason                                              │
│         │                                                                                        │
│         ├─ parsed ──────────────────────────────────────────────────────────┐                    │
│         └─ GARBLED ──►  ③  RE-REVIEW  (re-send the SAME rubric + SAME answer  │                  │
│                            + a format nudge)  ×eval_retries                   │                  │
│                             │                                                 │                  │
│                             ├─ now parseable ──────────────────────────────►│                    │
│                             └─ still garbled ──► fall back to score 0        ▼                   │
└──────────────────────────────────────────────────┬──────────────────────────────────────────────┘
                                                    ▼
                              score ≥ pass_score  OR  hit max_iters ?
                                    │                        │
                                   yes                       no
                                    │                        ▼
                                    │           ②  REFINE  (worker rewrites the ANSWER
                                    │              from best_answer + judge feedback) ──► back to EVALUATE
                                    ▼
                             STOP, return best answer
```

① and ③ are **plumbing** — they run *only* when something went wrong (no data / unparseable score),
and on a healthy run you never see them. ② **refine** is the **normal quality loop**.

---

## ① Reformulate + retry — "ask the question differently"

**When:** retrieval came back **blank** — the SQL tool returned no rows, Cortex Analyst produced no SQL
(e.g. *"the semantic model defines no relationships between these tables"*), or no declared tool produced
usable output. A pack may also opt in (`zero_is_no_data: true`) to count a single all-zero/NULL row —
`COUNT(*) = 0`, `SUM(...) = NULL` — as blank, since that is structurally a row but carries no information.

**Why it exists:** a blank result does **not** prove the data is missing. It might just mean the
question was **misread**, **over-scoped**, or asked for a join the semantic model doesn't support. So
before giving up, we give it a bounded chance to self-correct.

**What actually happens:** the worker LLM is shown the original question **plus the tool's hint about
why nothing came back**, and is asked to produce **one corrected question** (see
[`shared/prompts/reformulate.md`](../../shared/prompts/reformulate.md)). We send that new question back
to **retrieval** and try again — the single SQL call, or the deterministic tool sweep. Both fire exactly
once per run, which is what makes a second attempt meaningful; `tool_mode: agentic` is **excluded**
because it already self-corrects across its own `max_tool_steps`.

**The rewrite is NOT trusted blindly.** Asked to rephrase an unanswerable question, a model will happily
ask one the semantic model *can* answer — which returns rows, looks grounded, and earns a **passing
score for a question nobody asked**. Observed live: *"average employee salary by department"* came back
as *"average inventory value per plant"*, returned rows, and the honest *"this data has no employee
fields"* answer scored **16/18**. So three gates apply, and which one trips decides what the human is
told:

| Gate | What it catches | Escalates as |
|---|---|---|
| the prompt may reply `NO_REFORMULATION` | the subject isn't in this data **at all** | `out_of_scope` |
| `_keeps_subject` — a distinctive word of the original must survive | the subject was **substituted** | `out_of_scope` |
| `_keeps_pinned` — a pinned identifier (`ZZ999`, `PO12345`) must survive | the rewrite silently **widened** the population | `no_data` |

The last one is a different situation from the first two: asking about *plant `ZZ999`* **is** in scope —
only that code matched nothing — so the human must be told "no rows for that code", not "wrong
question". A tripped gate is **final**: the blank is kept, the run escalates, and the remaining retry
budget is **not** burned (re-asking the same model the same way won't help).

- **Recovered data?** → it was a misread. Proceed normally (write the answer, judge it, maybe refine).
- **Still blank after `max_data_retries`?** → a genuine data gap → escalate as `no_data`.
- **Rewrite refused or off-subject?** → escalate as `out_of_scope`.

Every escalation keeps the honest "I couldn't answer, here's why" text for the human and **skips the
judge entirely** — an escalated run is never scored, never "passes", and reports `score = None`. See
[end-to-end-flow.md → the three outcomes](end-to-end-flow.md#three-outcomes-of-a-run).

**Note what changes between attempts: the QUESTION** (the input to the tool). The worker is *not*
improving an answer here — there is no answer yet.

**Bound:** `max_data_retries` (default **1**, max 5; `0` = escalate immediately). Code:
`generate()` in [`engine/graph.py`](../../engine/graph.py).

---

## ② Refine — "improve the answer"

**When:** the judge scored the answer **below `pass_score`** and we haven't hit `max_iters` yet.

**Why it exists:** this is the core quality loop — the whole point of the agent. A first draft is
often mediocre; the judge's critique tells the worker exactly what to fix.

**What actually happens:** the worker LLM is given the **best answer so far** + **the judge's
feedback that produced it**, and writes a **new, improved answer** (see
[`shared/prompts/refine.md`](../../shared/prompts/refine.md)). That new revision goes back to the
judge to be re-scored.

Two things it does **not** do:
- It does **not** re-query the tool. The `data` fetched in generate is reused as-is — refine improves
  *how the answer uses the data*, not *what data we have*. (Fixing the data is ①'s job, and it
  happens earlier.)
- It does **not** build on the *latest* revision — it builds on the **best** one, so a bad revision
  can't drag the loop downhill. (See [the-refine-loop.md §2](the-refine-loop.md).)

**Note what changes between attempts: the ANSWER** (the worker's output). The question and the data
stay fixed.

**Bound:** `max_iters` (default 4), `pass_score` (stop when good enough), and `max_stall` (stop if
refines stop improving). Code: `refine()` / `keep_going()` in [`engine/graph.py`](../../engine/graph.py).

---

## ③ Re-review — "give me a readable score"

**When:** the judge replied, but its text **couldn't be parsed** into a `SCORE: N/max - reason`
verdict (e.g. it wrote a paragraph, or `SCORE: about 15ish`, or a malformed denominator).

**Why it exists:** the score decides which answer reaches the human, so an unreadable score can't be
silently treated as good. We give the judge another try to say the score in the required format.

**What actually happens:** we re-send the **entire rubric prompt, rebuilt from scratch** — same task,
same evidence, the **same worker answer** — with only a short format nudge appended: *"Your previous
reply could not be parsed. Reply with EXACTLY one line: SCORE: N/max - reason."* It is a **fresh
re-score of the unchanged answer**, not a "here's your garbled score, resend the number" message, and
**no worker is involved** — the answer does not change.

- **Now parseable?** → use that score.
- **Still garbled after `eval_retries`?** → fall back to **score 0** (fail-closed). A 0 is below
  `pass_score`, so it *forces a refine* rather than letting a broken verdict pass.

**Note what changes between attempts: nothing substantive** — just a format nudge. Same answer, same
rubric.

**Bound:** `eval_retries` (default 1). Code: `evaluate()` / `parse_verdict()` in
[`engine/graph.py`](../../engine/graph.py). Deeper dive: [the-refine-loop.md §6](the-refine-loop.md).

---

## Side-by-side

| | ① Reformulate + retry | ② Refine | ③ Re-review (garbled verdict) |
|---|---|---|---|
| **Fires when** | retrieval came back **blank** | answer scored **< `pass_score`** | judge's reply **can't be parsed** |
| **The problem it fixes** | we have **no data** | the **answer is weak** | the **score is unreadable** |
| **Who acts** | worker (rephrases the question) | worker (rewrites the answer) | judge (re-scores) |
| **What changes each try** | the **question** | the **answer** | **nothing** (just a format nudge) |
| **Re-queries the data tool?** | **yes** (with a new question) | no (reuses the same `data`) | no |
| **Does the judge run?** | not yet (only after data is recovered) | yes — that's what triggered it | yes — it *is* the judge |
| **Carries over** | original task + the tool's "why blank" hint | best answer + the feedback that produced it | same rubric + same answer |
| **Bounded by** | `max_data_retries` (default 1) | `max_iters` / `pass_score` / `max_stall` | `eval_retries` (default 1) |
| **When it gives up** | **escalate** (`no_data` or `out_of_scope`), skip the judge, no score | stop, return the **best** answer so far | fall back to **score 0** → forces a refine |
| **Where in the loop** | inside GENERATE, before scoring | between EVALUATE and the next EVALUATE | inside EVALUATE |

---

## One worked question that hits all three

**Question:** *"What is the total contract value by supplier?"* (pack: `procurement_contracts`,
`max_score` 18, `pass_score` 15)

**Step 1 — retrieve → blank.** Cortex Analyst can't build SQL and returns a hint:
> *"This is our interpretation… the semantic model does not define relationships between CONTRACTS
> and SUPPLIERS."*
The tool marks this **no-data** (not real rows).

**Step 2 — ① reformulate + retry.** The worker is shown the question + that hint and rephrases to
stay within one table:
> *"What is the total value of contracts grouped by the supplier-name column on the contracts table?"*
We re-query. This time Analyst produces valid SQL and returns rows:
> `SUPPLIER | TOTAL_VALUE`  ·  `Acme | 12,400,000`  ·  `Globex | 9,800,000`  · …
Data recovered → we continue. *(Had it stayed blank after `max_data_retries`, the run would have
ended here as `status="no_data"`, no score — handed to a human to add the relationship or rescope.)*

**Step 3 — generate the answer (rev0).** The worker writes:
> *"Total contract value is about $22M."*

**Step 4 — evaluate → ③ re-review.** The judge's first reply is garbled:
> *"Pretty good but it's missing the per-supplier breakdown, maybe fifteen-ish out of eighteen."*
`parse_verdict` can't read a score, so we **re-send the same rubric + same answer** with the format
nudge. The judge now replies cleanly:
> `SCORE: 11/18 - correct total but no per-supplier breakdown, no top contributors named.`
11 < 15 → not good enough.

**Step 5 — ② refine.** The worker rewrites the **answer** (same data, same question) using that
feedback:
> *"Total contract value is $22.2M across 14 suppliers. Top 3: Acme $12.4M (56%), Globex $9.8M,
> Initech $4.1M. The long tail (11 suppliers) is under $1M each."*
Back to the judge:
> `SCORE: 16/18 - accurate total, clear per-supplier breakdown and ranking.`
16 ≥ 15 → **stop.** Return this answer.

Notice how each retry touched a different thing: **① changed the question** to get data, **③ changed
nothing** but got a readable score, **② changed the answer** to make it better.

---

## The one that's genuinely different: escalation vs. the others

①, ② and ③ are all "retry the same run." **Escalation** (what ① does when it gives up) is the odd one
out — it's a **terminal outcome**, not a retry. It says: *the loop cannot proceed because there's no
data to reason about, so a human must act.* Which action depends on the reason:

- **`no_data`** — the query ran and matched **nothing**. Check the identifier, the filters, or data
  freshness. The question is answerable in principle; this population is empty.
- **`out_of_scope`** — this data **cannot** answer that question. Point the user at a different agent or
  semantic model; no amount of re-querying will help.

Both skip the judge and report no score — scoring an answer that had no data behind it is exactly the
bug this whole mechanism prevents. See [`CLAUDE.md`](../../CLAUDE.md) → "Groundedness is DETERMINISTIC."
