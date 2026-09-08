# Evals & the learning flywheel — how the agent improves without retraining

This guide explains two things people often conflate:

1. **What golden-set evals are** (and what they are *not* — they are tests, not training data).
2. **The learning flywheel** — how a real, logged failure turns into git-owned assets that
   make the next version better *and* prove it stayed fixed.

> **The one principle to hold onto (decision #1 in [`CLAUDE.md`](../../CLAUDE.md)):** nothing
> here ever retrains model weights. "Fine-tuning" an agent in this architecture means editing
> *config* — prompts, the rubric, verified `Q→SQL` exemplars, golden questions. All plain text
> in git. That is exactly why improvement survives a Snowflake↔Databricks migration: only text
> moves, and text is portable.

---

## 1. Evals are test cases, not training data

Golden-set evals **grade** the agent; they never train it. If you've written unit tests, you
already have the right mental model.

```
usecases/<pack>/evals/golden_set.yaml   ──►  run through the loop  ──►  PASS / FAIL
   (fixed question + expected facts)                                    (a grade; nothing is updated)
```

The eval is a **measuring stick**. You run it, you read the result, and *you* (the human)
decide what to change. The eval itself updates nothing.

### Why this feels like training (the native-platform confusion)

You may have seen "evals" in Snowflake / Databricks / OpenAI tooling and assumed they feed
back into the model. Here's the honest breakdown of what evals are actually used for:

| Use | Retrains weights? | What happens |
|---|---|---|
| **Regression testing** | No | "Did my change break answers?" — same as here. |
| **Prompt / config tuning** | No | Read the failures, edit the prompt or add a verified query, re-run. Human-in-the-loop, not automatic. |
| **Model selection** | No | Run the same evals against different models; pick the winner. |
| **Actual fine-tuning** | **Yes** — but it's a *separate* product/step | Export a `Q→ideal-answer` dataset and run a fine-tune job. The eval set is often *reused* as that dataset, which is why the two blur together. |

The blur is legitimate: **the same `Q→expected` YAML can serve two jobs** — grading, and (if
you choose) seeding a fine-tune. But those are different actions. Most native "eval" usage is
grading. True fine-tuning is a deliberate, separate, paid training run.

**In this repo we only grade.** We never fine-tune — if improvement lived in trained weights,
it would be locked inside one vendor and would not survive migration.

### The three "check" layers (don't confuse them)

| Layer | Runs when | Granularity | Purpose |
|---|---|---|---|
| **Judge** (`evaluate` node, `eval_llm`) | every request, mid-loop | 0–`max_score` rubric score | drive refine; decide "good enough" |
| **Golden-set evals** (`run_evals.py`) | on demand / CI | pass/fail per question | regression gate across changes |
| **Unit tests** (`tests/`, pytest) | on demand / CI | code assertions | verify the *machinery* (parsing, memory, SQL guard) |

Judge = the **inner** per-answer quality loop. Evals = the **outer** behavioral test suite.
Pytest = the **code** correctness suite.

---

## 2. The learning flywheel — a real failure, end to end

"Grow the eval set from the memory flywheel" means: take questions your users actually asked
(logged by [`engine/memory.py`](../../engine/memory.py)), and hand-promote the important ones
into git-owned test/tuning assets. Here is the full pipeline on one concrete failure.

```
1. RUNTIME     every finished run + any 👍/👎  →  append-only rows      (engine/memory.py)
                          │
2. YOU CURATE  (by hand)  │  skim the rows: find good answers worth locking in,
                          ▼  and bad ones worth guarding against
3. GIT ASSETS  ┌──────────────────────────┬───────────────────────────┐
               │ exemplars/*.yaml          │ evals/golden_set.yaml      │
               │ (verified Q→SQL to reuse) │ (Q→expected to test)       │
               └──────────────────────────┴───────────────────────────┘
```

### Step 1 — A real run gets logged (automatic)

A user asks a question in production. `remember_run()` writes one episodic row to
`PORTABLE_AGENT_INTERACTIONS`:

| RUN_ID | QUESTION | ANSWER | SCORE | ITERATIONS |
|---|---|---|---|---|
| `a1f9…` | "Which QALS lots are duplicated more than twice?" | "There are 3 duplicated lots: 40001122, 40001123, 40001124." | 11 | 2 |

A 👎 lands in `PORTABLE_AGENT_FEEDBACK` (via the Snowflake shell's `POST /feedback`):

| RUN_ID | RATING | NOTE |
|---|---|---|
| `a1f9…` | `down` | "Wrong — I asked for *more than twice* (3+ occurrences); it listed lots that appear exactly twice." |

Two signals — a **low judge score (11)** and a **human 👎**. That's your candidate queue.
Nothing past this point is automated: *you* decide what to promote.

### Step 2 — You triage the row

You investigate and confirm the agent misread "more than twice." The correct answer: only lot
`40001124` appears 3 times; the others appear exactly twice and shouldn't be listed. This is a
subtle phrasing real users hit — worth locking in.

The row now branches into **two different git assets**, depending on what you want it to do.

### Step 3a — Promote to a GOLDEN EVAL (guard against the regression)

You want a permanent test that fails until the agent gets this phrasing right. Add to
`usecases/dq_qals/evals/golden_set.yaml`:

```yaml
- question: "Which QALS lots are duplicated more than twice?"
  expect_contains: ["40001124", "3"]   # only the lot with 3+ occurrences; not the twice-only ones
```

**What it does:** every future `python run_evals.py dq_qals` runs this question through the
full loop and asserts the answer names `40001124`. The day a prompt edit reintroduces the
bug, this eval goes **FAIL** and you catch it before shipping. It's a tripwire.

### Step 3b — Promote to an EXEMPLAR (teach the fix, few-shot)

An eval only *detects* the problem. To actually *fix* it, give the generator a worked example.
Add to `usecases/dq_qals/exemplars/verified_queries.yaml`:

```yaml
- question: "Which inspection lots are duplicated more than twice?"
  sql: "SELECT PRUEFLOS, COUNT(*) AS N FROM QALS GROUP BY PRUEFLOS HAVING COUNT(*) > 2"
```

**What it does:** this pair is injected into `generate.md` as few-shot context, so next time
the model sees "more than twice → `HAVING COUNT(*) > 2`" (not `> 1`). It's the correction —
and the seed you'd hand to Cortex/Genie as a *verified query* on each platform.

### How the two fit together

```
👎 logged failure
      │
      ├──► evals/golden_set.yaml   →  DETECTS the bug forever   (the tripwire / test)
      │
      └──► exemplars/*.yaml        →  FIXES the bug next run     (the correction / few-shot)
```

- Add the **exemplar** → the agent should now answer correctly.
- Add the **eval** → you can *prove* it does, and prove it stays fixed.

For an important failure you usually do both: one teaches, one guards. All of it is plain YAML
edits in git — **zero retraining.** Move to Databricks tomorrow and both files come with you;
the fix and the test survive the migration.

---

## 3. The rule: curate, don't auto-dump

Curation is **manual and selective** (decisions #3 and #4 in [`CLAUDE.md`](../../CLAUDE.md)).
Do **not** pipe every logged run into the eval/exemplar sets:

- It pollutes the golden set with noise, so a FAIL stops meaning something.
- Most runtime SQL is disposable — it regenerates identically on any platform, so owning it
  buys nothing.
- Exemplars are few-shot seed; a bloated set dilutes the signal and costs tokens every call.

Bless a **handful**. "Own it or lose it" applies only to the high-value, hard-to-rebuild
cases — the subtle phrasings, the domain gotchas, the questions users actually get wrong.

---

## TL;DR

- **Evals grade the agent (like unit tests); they never train it.** This repo has no training
  step at all — improvement is config, not weights.
- **The flywheel:** real usage → logged rows (`engine/memory.py`) → *you* hand-pick the
  important failures → promote each into a **golden eval** (a permanent regression tripwire)
  and/or an **exemplar** (a few-shot correction).
- **Curate, don't auto-dump.** Bless a handful; the value is a small, high-signal, git-owned
  set that survives any platform move.

## See also

- [`GLOSSARY.md`](../../GLOSSARY.md) — terms (exemplar, verified query, judge, verdict).
- [`PROJECT_CONTEXT.md`](../../PROJECT_CONTEXT.md) — the decision log and the "own it or lose it" thesis.
- [`CLAUDE.md`](../../CLAUDE.md) — operational spec; memory & observability modules.
