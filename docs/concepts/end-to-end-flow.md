# End-to-end flow — which artifact is used, and when

A map of every moving part (`config`, `skills`, `prompts`, `rubric`, `refine`, `exemplars`,
`tools`/SQL, `evals`, `tests`) and **exactly when each one is touched**.

The one insight to hold onto: there are **two separate flows**, and not everything is in both.

1. **Runtime flow** — serves a live user question. Runs every time someone asks the agent something.
2. **Test-time flow** — *you* (or CI) run it to grade/verify the agent. Never triggered by a user.

> Your instinct — *"evals are like unit testing; they run during testing, not during actual usage"* —
> is **correct for the golden-set evals** (`evals/golden_set.yaml`, run by `run_evals.py`). It is **not**
> true for the **judge** (the `evaluate` step): that runs on *every* live question, because it's the
> self-check that decides whether to refine. See ["Eval means two things"](#eval-means-two-different-things) below.

---

## 0. Loaded once, before any question (the "build" step)

When the agent starts (`build_graph` in [`engine/graph.py`](../../engine/graph.py)), it composes the pack
with the shared tier and holds it in memory:

```
config.yaml (+ inherits: [shared])   →  scale, thresholds, tool list
skills/     (shared + pack)           →  concatenated
prompts/*.md (pack ELSE shared)       →  generate.md / rubric.md / refine.md
exemplars/*.yaml (pack)               →  few-shot examples
tools: / SQL                          →  the evidence source
```

None of this calls a model yet. It's just loading the text that the runtime flow will use.

---

## 1. Runtime flow — one user question

```
user question
     │
     ▼
┌─────────────────────────────┐
│ GATHER EVIDENCE             │  read-only tools  OR  sql.ask()
│  → data / observations      │
└──────────────┬──────────────┘
               ▼
┌─────────────────────────────┐
│ GENERATE   (worker llm)     │  uses: generate.md + skills + exemplars + tools + data
│  → answer rev0              │
└──────────────┬──────────────┘
               ▼
┌─────────────────────────────┐
│ EVALUATE   (judge eval_llm) │  uses: rubric.md + data
│  → SCORE + feedback         │
└──────────────┬──────────────┘
               ▼
   score ≥ pass_score?  OR  hit max_iters?  OR  hit max_stall?
        │ yes                                   │ no
        ▼                                       ▼
  return BEST answer            ┌─────────────────────────────┐
                               │ REFINE    (worker llm)      │  uses: refine.md + skills
                               │  → improved answer          │        + best answer + feedback + data
                               └──────────────┬──────────────┘
                                              │
                              back to EVALUATE ┘

side effects on every run:   tracer → timed events (event table / stdout)
                             memory → one interaction row (question, answer, score)
```

What this shows:

- **Evidence first, deterministically.** The model never *picks* a tool — the engine runs the pack's
  read-only tools (or the single SQL call) up front and hands the result in as `data`. (Injection-safe:
  tool output can't trigger another tool call.)
- **`generate` → `evaluate` → `refine`** is the loop from [`the-refine-loop.md`](the-refine-loop.md).
  `skills` shape both the first draft **and** every rewrite; `exemplars` seed only the first draft;
  the `rubric` grades; the judge's critique becomes refine's `feedback`.
- **It always returns the *best* answer seen**, with its score — not necessarily the last one.

---

## 2. Test-time flow — you / CI (never a live question)

```
YOU or CI
   │
   ├─ python run_evals.py <pack>
   │     └─ evals/golden_set.yaml
   │           └─ run each fixed question through the SAME runtime loop above
   │                 └─ compare best answer to expect_contains  →  PASS / FAIL
   │                    (a grade; nothing in the agent is updated)
   │
   └─ pytest tests/
         └─ assertions on the machinery: verdict parsing, tool dispatch, memory,
            HTTP safety, and artifact wiring — no live models, no credentials
```

- **`run_evals.py`** reuses the *entire* runtime loop; it just feeds it fixed questions and checks the
  answer. It's a behavioral regression gate.
- **`pytest`** never runs the models — it checks the *code* (parsing, safety, and that each artifact
  actually reaches the prompt: `tests/test_pack_artifacts_wired.py`).
- Neither one is on the path when a user asks a question.

---

## "Eval" means two different things

This is the single most common point of confusion, so it's worth stating plainly:

| "eval" | what it is | when it runs | trains anything? |
|---|---|---|---|
| **the judge** (`evaluate` step, `rubric.md`, `eval_llm`) | scores one answer 0–`max_score` to drive refine | **every live question**, mid-loop | no |
| **golden-set evals** (`evals/golden_set.yaml`, `run_evals.py`) | pass/fail regression tests over fixed questions | **on demand / CI only** | no |

Same word, two jobs: one is the **inner** per-answer quality gate (runtime); the other is the **outer**
behavioral test suite (test-time). Neither retrains model weights — improvement here is always *config*,
never trained parameters (see [`evals-and-the-learning-flywheel.md`](evals-and-the-learning-flywheel.md)).

---

## Artifact → when it's used (the summary table)

| artifact | what it does | in the **runtime** path (live question)? | **test-time** only? |
|---|---|---|---|
| `config.yaml` (+ `inherits`) | scale, thresholds, tool list | ✅ loaded at start | — |
| `skills/*.md` | domain/house rules, injected into generate **and** refine | ✅ | — |
| `prompts/generate.md` | the GENERATE step (always pack-owned) | ✅ | — |
| `prompts/rubric.md` | the EVALUATE (judge) step | ✅ every question | — |
| `prompts/refine.md` | the REFINE step | ✅ when below `pass_score` | — |
| `exemplars/*.yaml` | few-shot examples in generate | ✅ | — |
| `tools:` / SQL | gather evidence before generate | ✅ | — |
| tracer + memory | per-run events and one logged row | ✅ (side effects) | — |
| `evals/golden_set.yaml` | graded by `run_evals.py` | — | ✅ you / CI |
| `tests/` (pytest) | verify the machinery | — | ✅ you / CI |

Rule of thumb: **everything under `usecases/<pack>/` except `evals/` is loaded for a live question** —
though not every load runs the same way each time: `generate.md`, `skills`, `exemplars`, and the
evidence step run on the first draft; `rubric.md` runs on every evaluate; `refine.md` (and skills again)
run **only when a score is below `pass_score`** and iterations remain. `evals/` (and the repo-level
`tests/`) are the safety nets you run yourself, never on the live path.

---

## See also

- [`the-refine-loop.md`](the-refine-loop.md) — the generate→evaluate→refine loop, the prompts and their
  `{placeholders}`, refine vs. rubric, and the stop conditions.
- [`evals-and-the-learning-flywheel.md`](evals-and-the-learning-flywheel.md) — golden-set evals vs. the
  judge, and how logged failures become git-owned tests/exemplars.
- [`tools-and-agents.md`](tools-and-agents.md) — how the evidence step (tools / SQL) works.
- [`GLOSSARY.md`](../../GLOSSARY.md) — terms: skill, exemplar, verified query, judge, verdict, rubric.
