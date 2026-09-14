# The refine loop — how the agent improves an answer (and when it stops)

This explains the generate→evaluate→refine loop in [`engine/graph.py`](../../engine/graph.py):

1. **What "refine from best" means** and why the loop works that way (§2).
2. **The prompts + `{placeholders}`** behind each step (§3).
3. **The two scoring knobs** — `max_score` vs `pass_score` — and when it stops (§4).
4. **What "refine" actually re-runs** — it improves the existing answer, it does *not* restart (§5).
5. **What happens when the judge's verdict can't be parsed** — retry the judge, then fail safe (§6).

> **Confused by "refine" vs. the other retries?** The loop retries in three different places for
> three different reasons — reformulate the *question* (on blank data), refine the *answer* (on a low
> score), re-review the *score* (on a garbled verdict). If those blur together, read
> [**retries-explained.md**](retries-explained.md) first — it's a comparison table + one worked
> question that hits all three. This doc then goes deep on **refine** specifically.

---

## 1. The loop at a glance

```
question
   │
   ▼
GENERATE ──► answer (rev0)
   │
   ▼
EVALUATE ──► judge scores it 0..max_score, writes a critique (feedback)
   │
   ├─ score ≥ pass_score  OR  hit max_iters  ──►  STOP, return best answer
   │
   └─ not yet ──► REFINE ──► improved answer ──► back to EVALUATE
```

Each **refine** step takes *an* answer plus the judge's feedback and rewrites it. The question
this doc answers: **which answer does refine build on?**

### Who does what (don't conflate these)

- **`refine` is a WORKER step** — it produces a **new, improved answer**. It is NOT the evaluator
  re-reading the same text. Each refine yields a *different* revision, which the judge then grades.
- **The evaluator is the grader.** We don't "help" it — the *help flows the other way*: the judge's
  **feedback** is handed to the **worker** so it knows what to fix. Flow: *judge feedback → worker →
  refined answer → judge grades the refined answer.*
- **Re-reading the SAME answer** happens only in the **garbled-verdict retry** (§6) — a judge-only
  re-score because its previous reply couldn't be parsed. That is not refine; no worker is involved.
- **Which answer refine builds on:** the **best so far**. On the *first* refine there is only rev0
  (from `generate`), so "best" = rev0; on later rounds it's the highest-scoring revision (see §2).

### A worked example (two iterations)

**Question:** "What is the total on-hand inventory balance by location?"
**Evidence (from the tool/SQL step, i.e. `{data}`):**
```
LOCATION | ON_HAND
SD-01    | 12340
SD-02    | 8915
SG-01    | 4220
```

| step | who | output |
|---|---|---|
| generate (rev0) | worker | "There's on-hand inventory across a few locations." |
| evaluate | judge | `SCORE: 11/18 - no total, no per-location numbers, doesn't name the largest` |
| keep_going | engine | 11 < 15 (pass_score) and iterations left → **refine** |
| refine (rev1) | worker | *given rev0 (the best so far) + that feedback →* "Total on-hand is **25,475** units across 3 locations: SD-01 (12,340), SD-02 (8,915), SG-01 (4,220). SD-01 holds ~48% — the largest." |
| evaluate | judge | `SCORE: 16/18 - complete and quantified; names the largest` |
| keep_going | engine | 16 ≥ 15 → **STOP**, return rev1 (16/18) |

The judge graded **two different answers** (rev0, then rev1) — never the same text twice. Its
critique of rev0 became refine's `{feedback}`, which is how the worker built rev1.

---

## 2. "Refine from best," not "refine from latest"

The loop tracks two things as it runs:

| field | meaning |
|---|---|
| `answer` | the **latest** revision (whatever refine just produced) |
| `best_answer` | the **highest-scoring** revision seen so far |
| `best_feedback` | the judge's critique of `best_answer` (added for this behavior) |

**Refine builds from `best_answer` (+ `best_feedback`), not from the latest `answer`.**

### Why — the failure it prevents

LLM refinement isn't guaranteed to improve. A rewrite can *introduce* an error and score
**worse** than the revision before it. If the loop always refined from the *latest* answer, one
bad rewrite would become the base for the next one — and the loop could **walk downhill**,
spending full (and slow, and paid) model calls building on a degraded answer.

### Before vs after (concrete)

Question → the judge scores each revision. Say `pass_score` isn't reached yet, so it keeps going:

| iter | refine builds on… | new score | best so far |
|---|---|---|---|
| rev0 (generate) | — | **16** | rev0 (16) |
| rev1 | rev0 | 10  ⟵ *a rewrite that got worse* | rev0 (16) |
| rev2 | **← here's the difference** | | |

- **Old behavior (refine from latest):** rev2 builds on **rev1 (the 10)** — trying to fix a
  broken answer. It might crawl back to 12, still worse than the 16 you already had. The good
  rev0 was abandoned.
- **New behavior (refine from best):** rev2 builds on **rev0 (the 16)** with rev0's critique —
  another attempt to improve the *good* answer. Much more likely to beat 16.

### The mental model: greedy hill-climbing

- Refine-from-latest = a **random walk** — it can wander downhill and get lost.
- Refine-from-best = **always restart from the highest point you've found**. You never lose your
  best work, and every new attempt tries to top the current champion.

### Why `best_feedback` had to come along

The feedback you hand to refine must describe the answer you're improving. If we refined from the
*best* answer but handed it feedback about a *different* (worse) answer, the instructions wouldn't
match the text — incoherent. So we store the critique that produced `best_answer` and reuse it.

### What was already true vs. what changed

- `best_answer` was **always** tracked, and the loop **always** returned it at the end (so the
  final output was never the degraded one). That safety net is unchanged.
- The change: `best_answer` is now also the **building block** for the next refine — not just the
  thing returned at the end. (Plus we now also carry `best_feedback`.)

### First refine is identical

On the first refine there's only one answer (rev0), so best == latest and nothing differs. The two
behaviors only diverge once a rewrite has scored worse than an earlier one — i.e. exactly the
downhill case this protects against. If every rewrite improves, the behavior is the same as before.

### The one edge to know — and its guard

Could it get "stuck" — repeatedly refining the same best answer with the same feedback and never
beating it? With a **deterministic / low-temperature** worker, yes: same base + same critique →
same output → same score → it never beats best, and it would grind through every `max_iters` round
producing identical revisions (full paid LLM calls for zero movement).

So there's a code guard: **`max_stall`** (default 2). The loop counts consecutive refines that
*don't* beat `best_score`, and stops once that hits `max_stall`. A refine that *does* improve resets
the counter. Set `max_stall: 0` in config to disable it. (This is separate from `max_iters`, the
absolute cap; whichever triggers first ends the loop.)

### If a refinement fails outright

A refine call can also *fail* (e.g. the worker truncates or errors mid-loop). That is **non-fatal**:
the loop keeps the `best_answer` it already scored and stops, rather than throwing away good work
with an error. Only a failure on the **first** `generate` is fatal — there's nothing to fall back
to yet.

> **Do not "simplify" refine back to `s["answer"]`.** That reintroduces the downhill-drift bug.
> This is noted in [`CLAUDE.md`](../../CLAUDE.md) conventions.

---

## 3. The prompts behind each step — and how the `{placeholders}` fill

Each box in the loop is driven by a **prompt template** — a `.md` file with `{placeholders}` in it.
You author the template once; **the engine fills the `{...}` slots at runtime**. There is nothing to
"enter" by hand: `{revision}`, `{task}`, `{data}` and the rest are substituted on every call from the
live run state.

| step | template | model that runs it | what it produces |
|---|---|---|---|
| GENERATE | `prompts/generate.md` | worker (`llm`) | the first answer (rev0) |
| EVALUATE | `prompts/rubric.md` | judge (`eval_llm`) | a score + a critique |
| REFINE | `prompts/refine.md` | worker (`llm`) | an improved answer |

### The placeholders, and where each value comes from

All substitution happens in `fill()` ([`engine/graph.py`](../../engine/graph.py)). It runs in a
**single pass** and is **brace-safe**: any `{name}` the engine doesn't supply (or a literal `{` in a
SQL/JSON example) is left untouched — an unknown placeholder is a harmless no-op, never an error.

| placeholder | the engine fills it with | appears in |
|---|---|---|
| `{task}` | the user's question | generate, rubric, refine |
| `{data}` / `{observations}` | evidence from the tools/SQL step (`{observations}` is a domain-neutral alias for the same string) | generate, rubric, refine |
| `{skills}` | shared + pack skill files, concatenated | generate, refine |
| `{exemplars}` | the pack's few-shot examples | generate |
| `{tools}` | a description of the pack's tools (`"None."` for a SQL/legacy pack) | generate |
| `{answer}` | in **rubric**: the candidate being graded; in **refine**: the best answer so far | rubric, refine |
| `{feedback}` | the judge's critique of that best answer | refine |
| `{revision}` | the revision number (0, 1, 2 …) | generate (0), refine (n) |
| `{max_score}` | the rubric scale from config (e.g. 18) | rubric |

> So `{revision}` in `refine.md` is **filled by the engine** with the current round number — it is not
> something you set. The same is true of every other `{...}`. You edit the *wording* around the
> placeholders; the engine supplies the *values*.

### "Small token cost" — what that meant

When `{skills}` was added to `refine.md`, every **refine** call now also carries the skills text. That
is a **recurring** per-refine cost (skills are re-sent on each rewrite), **not** a one-time charge — but
it is small because skill files are short, and it is **zero when a run passes on rev0** (no refine runs
at all — exactly what happened in the live inventory run, `iterations: 0`). A hard question that refines
three times sends the skills three times. The upside: every rewrite keeps following the house/domain
rules instead of drifting away from them after the first draft.

## Refine vs. the rubric — two different jobs

These are easy to conflate because they sit next to each other in the loop, but they play opposite roles:

| | **rubric.md** (EVALUATE) | **refine.md** (REFINE) |
|---|---|---|
| Role | **grades** the answer | **rewrites** the answer |
| Run by | the **judge** (`eval_llm`) | the **worker** (`llm`) |
| Output | `SCORE: N/max - reason` | a better answer |
| Runs | **every** question, mid-loop | **only** when the score is below `pass_score` and iterations remain |
| Reads | the candidate answer + the evidence | the *best* answer + the judge's *feedback* + skills + evidence |

The link between them: the judge's `reason` becomes refine's `{feedback}`. **The rubric says what's
wrong; refine fixes it.** One is the examiner, the other is the student revising to the examiner's notes.

### Shared vs. per-pack (the "override")

Prompts resolve **pack-first, then shared** (`_prompt()` in `engine/graph.py`): if
`usecases/<pack>/prompts/refine.md` exists it is used; otherwise `shared/prompts/refine.md`. Today
**no pack overrides `refine.md`**, so every pack uses the shared one — which is why editing
`shared/prompts/refine.md` changes the refine step for all packs uniformly. If one pack ever needs a
different revision style, drop a `refine.md` into that pack's `prompts/` and it overrides shared **for
that pack only** — the same mechanism used for `rubric.md` (e.g. `dq_qals` and `inventory_balance` ship
their own `rubric.md`; the rest inherit shared). `generate.md` is always pack-owned — there is no shared
default for it.

---

## 4. The two scoring knobs: `max_score` vs `pass_score`

These are often confused. They are different things.

| knob | default | what it is | analogy |
|---|---|---|---|
| `max_score` | 18 | the **scale / ceiling** — the judge scores 0..max_score | the test is out of 18 |
| `pass_score` (alias `threshold`) | 15 | the **stop bar** — refining stops once a score is ≥ this | "an 83% is good enough" |

Key points:

- **`pass_score` does not cap the score.** A best answer can score all the way up to `max_score`
  (18). `pass_score` only decides when the loop is satisfied enough to stop.
- **The loop stops at the *first* answer that reaches `pass_score`.** So if `pass_score=15` and a
  draft scores 16, it stops at 16 — it won't keep refining to chase an 18.
- **Why the default is 15, not 18.** A strict judge rarely gives a perfect 18. If `pass_score`
  equalled `max_score`, almost every question would run all the way to `max_iters` (the maximum
  number of refine rounds) — making the worst-case latency and cost the *normal* case. 15/18 is
  "average 5 out of 6 on each of the three axes": solidly good, and reached early.

### Tuning it

- Want **higher quality** (push answers closer to perfect)? Raise `pass_score` toward 18 — costs
  more model calls.
- Want it **faster / cheaper**? Lower `pass_score` — stops sooner, at a lower bar.
- Set it **per pack** in that pack's `config.yaml`, or change the shared default in
  [`shared/config.yaml`](../../shared/config.yaml). Must stay ≤ `max_score`.

The other stop condition is `max_iters` (default 4): the loop also stops after that many refine
rounds even if it never reaches `pass_score` — a safety cap so a hard question can't loop forever.
Either condition ends the loop; you always get back the **best** answer seen, with its score.

> Scores are always reported out of **`max_score`** (e.g. `16/18`), never out of `pass_score`.

---

## 5. What "refine" actually re-runs (it does NOT restart)

A common misread: "loop to refine" sounds like the whole thing starts over. It doesn't.

- **`generate` runs exactly once**, at the very start (rev0).
- After that the cycle is **`refine` ↔ `evaluate` only** — it **never** goes back to `generate`.
- `refine` does **not** ask the worker for a brand-new answer from a blank page. It hands the worker
  the **existing best answer** (`best_answer` + `best_feedback`) and says *"improve THIS, here's the
  critique,"* producing a revised version — which then goes to `evaluate` for re-review.

```
generate (once) ─► evaluate ─►(score < pass_score, iters left)─► refine ─► evaluate ─► … ─► STOP
                      ▲                                             │
                      └───────────── re-review the revision ────────┘
   refine = take the EXISTING best answer + feedback → rewrite it → re-evaluate   (never re-generate)
```

So: **existing answer → apply refine → re-evaluate.** The worker improves; it never starts over.

## 6. When the judge's verdict can't be parsed

"We" here is the **`evaluate` node**. It calls the judge, then parses the judge's *free-text reply*
(a JSON object **or** the `SCORE: N/max - reason` line form — the reply is the judge *model's* free
text, which we don't control, so it's parsed defensively; see the `parse_verdict` docstring). The
denominator (`/18`) is fixed by **us** in the rubric prompt; we just verify the judge echoed it and
didn't quietly switch scales (`85/100`). If the reply is unparseable or out of range:

1. **Re-ask ONLY the judge** — not the whole loop, and **not** `generate`. We re-send the **entire
   rubric prompt rebuilt from scratch** — task + evidence + the **same worker answer** + scoring
   instructions — with only a format nudge appended: *"Your previous reply could not be parsed. Reply
   with EXACTLY one line: SCORE: N/max - reason."* Up to `eval_retries` times (default 1 → 2 tries).
   It is a **fresh re-score of the same answer**, NOT a "here's your garbled score, resend the
   number" message: we don't have a parseable score to correct, and feeding the garbled reply back
   to let the model self-interpret is fragile and re-opens the misread risk. The nudge constrains the
   *format* only; the judge grades the same answer again.
2. **Still unparseable after the retries → fall back to a safe `Verdict(score=0)`.** Zero is below
   `pass_score`, so `keep_going` routes to **refine** — i.e. improve the existing answer and try
   again — bounded by `max_iters`.

**Two failure kinds are handled differently on purpose:**

| the judge call… | handling |
|---|---|
| returns a **blank / whitespace** reply (`EmptyResponseError`) | retried, then falls back to 0 (transient) |
| returns **unparseable / out-of-range** text | retried with the format nudge, then falls back to 0 |
| raises a **config/auth error** (e.g. bad key, `LLM_MAX_TOKENS=abc`) | **propagates and fails loud** — a real misconfiguration is never masked as a score of 0 |

**Honest trade-off of the 0-fallback:** if the *answer* was fine but the *judge* merely misformatted
its reply, scoring it 0 triggers an (unnecessary) refine of a good answer. We accept that because it
is **safe** (a garbled verdict can never count as "good enough" and reach the human), **bounded**
(`max_iters`), and **rare** (a well-prompted judge on the one-line format almost always parses first
try). The parser is **fail-closed**: anything it can't read cleanly becomes a retry, then a 0 — never
a lucky passing number. (The line-form fallback is also hardened against garbled numerics like
`18e3` or a malformed denominator like `18/18e3` — those are rejected, not misread; see
`parse_verdict`.)

---

## See also
- [`end-to-end-flow.md`](end-to-end-flow.md) — the whole-system map: which artifact is used, and when
  (runtime vs. test-time).
- [`evals-and-the-learning-flywheel.md`](evals-and-the-learning-flywheel.md) — how the judge's score
  differs from the offline golden-set evals.
- [`CLAUDE.md`](../../CLAUDE.md) — the operational conventions for the loop.
- [`engine/graph.py`](../../engine/graph.py) — `generate` / `evaluate` / `refine` / `keep_going`.
