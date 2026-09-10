# The refine loop — how the agent improves an answer (and when it stops)

This explains two things about the generate→evaluate→refine loop in
[`engine/graph.py`](../../engine/graph.py):

1. **What "refine from best" means** and why the loop works that way.
2. **The two scoring knobs** — `max_score` vs `pass_score` — and how they decide when to stop.

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

### The one edge to know

Could it get "stuck" — repeatedly refining the same best answer with the same feedback and never
beating it? In theory, yes; in practice: (a) real models are stochastic, so each attempt differs;
(b) `max_iters` bounds the total attempts; and (c) refine-from-latest would be *worse* here — it'd
drift further down instead of holding at the best. So this caps the downside rather than creating one.

> **Do not "simplify" refine back to `s["answer"]`.** That reintroduces the downhill-drift bug.
> This is noted in [`CLAUDE.md`](../../CLAUDE.md) conventions.

---

## 3. The two scoring knobs: `max_score` vs `pass_score`

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

## See also
- [`evals-and-the-learning-flywheel.md`](evals-and-the-learning-flywheel.md) — how the judge's score
  differs from the offline golden-set evals.
- [`CLAUDE.md`](../../CLAUDE.md) — the operational conventions for the loop.
- [`engine/graph.py`](../../engine/graph.py) — `generate` / `evaluate` / `refine` / `keep_going`.
