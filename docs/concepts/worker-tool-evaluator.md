# Worker, tool, evaluator — the three actors (and the pluggable "middle node")

A common mental model is that the *worker* does everything: frame the question, generate SQL, query
the database, and write the answer, then hand it to the *evaluator* to judge. That is **not** how the
loop works. Data access is a **separate, pluggable step** — the "middle node" — that sits between the
worker and the evaluator. This doc makes that split explicit.

> Prereqs: [`the-refine-loop.md`](the-refine-loop.md) (the generate→evaluate→refine loop) and
> [`tools-and-agents.md`](tools-and-agents.md) (the tool layer the middle node is built on).
> For *which provider* runs the worker/evaluator (Cortex, Databricks, LiteLLM), see
> [`ai-gateway.md`](ai-gateway.md) — that is orthogonal to the middle node described here.

---

## 1. The three actors

```
question
   │
   ▼
┌─ GENERATE  (worker LLM) ────────────────────────────────────────────────┐
│   1. frame      : worker rewrites the question (NL → NL)                 │
│   2. retrieve   : the RETRIEVER / TOOL fetches data   ◄── the middle node │
│                   (this is what actually does text→SQL→run→rows)         │   ← PLUGGABLE
│   3. synthesize : worker writes the answer FROM the returned rows        │
└─────────────────────────────────┬────────────────────────────────────────┘
                                   ▼
                          EVALUATE  (evaluator LLM) ── scores 0..max + critique
                                   │
              score ≥ pass_score  OR  hit max_iters  ──►  STOP, return best answer
                                   │
                                   └─►  REFINE (worker LLM) ─►  back to EVALUATE
```

- **Worker LLM** (`rt.llm`) — frames the question, **synthesizes** the answer from the rows, and
  **refines** it. It does **not** write SQL or connect to the database.
  (`engine/nodes.py`: `frame_question` L58, `generate` synth L121, `refine` L187.)
- **Retriever / tool** — the **middle node**. Called *inside* `generate` at `nodes.py:118`
  (`rt.retriever.retrieve(...)`). This is the component that acquires the evidence. For an AI+BI pack
  it is **Cortex Analyst**, which is the thing that turns the framed question into SQL, runs it, and
  returns rows.
- **Evaluator LLM** (`rt.eval_llm`) — grades the answer and writes the critique (`nodes.py:145`). The
  critique flows back to the **worker** on `refine` (see [`the-refine-loop.md`](the-refine-loop.md)).

So in a mixed-provider AI+BI run (worker = Databricks, evaluator = Cortex): the Databricks worker
*framed* the question, **Cortex Analyst generated + ran the SQL** and returned rows, the Databricks
worker *wrote the answer from those rows*, and Cortex *judged* it. The worker never touched Snowflake.

---

## 2. Is the middle node always Cortex? No — it is pluggable

The retriever is chosen from the pack's config in `engine/graph.py:_build_retriever` (L122):

| Pack type | Retriever | Middle node (data engine) |
|---|---|---|
| **AI+BI (SQL)** | `SqlRetriever` | `SQL_TOOL=` **`cortex`** (Cortex Analyst) **or `genie`** (Databricks Genie) or `mock` |
| **API / MCP (multi-step)** | `PlannedRetriever` / `AgenticRetriever` | your declared HTTP / MCP tools |
| **generic (e.g. `generic_math`, `generic_joke`)** | `ToollessRetriever` | **none — there is no middle node** |

That is why the toolless example packs send the worker's output straight to the evaluator: no tool,
no data step, so `grounded` is always true and nothing can escalate as `no_data`.

For AI+BI, **the worker does not generate the SQL** — Cortex Analyst (or Genie) does. Which one runs
is set by `SQL_TOOL` / the pack's `semantic_layer.yaml`, independent of the worker/evaluator provider.

---

## 3. By design, not an accident

Separating "text→SQL over a governed semantic model" from the worker is the grounding strategy:

- **Accuracy / grounding** — Cortex Analyst / Genie generate SQL from the **semantic model**
  (verified queries, defined metrics and joins, read-only enforcement). An arbitrary worker LLM does
  not know the schema and would hallucinate columns.
- **Portability** — the generate→evaluate→refine loop is provider- and tool-agnostic. Swap
  Cortex ↔ Genie ↔ an API ↔ nothing by editing config; the worker and evaluator are untouched.
- **Three models in one AI+BI answer** — worker (frame + write), **Cortex Analyst (text→SQL)**, and
  evaluator (judge). Only the worker and evaluator are the pluggable *providers* you pick per pack
  (see [`ai-gateway.md`](ai-gateway.md)); the SQL tool is bound to the data platform.

---

## See also
- [`the-refine-loop.md`](the-refine-loop.md) — how `generate`/`evaluate`/`refine` iterate and stop.
- [`tools-and-agents.md`](tools-and-agents.md) — the typed tool layer, deterministic/agentic/planned modes.
- [`ai-gateway.md`](ai-gateway.md) — which provider runs the worker/evaluator (Cortex · Databricks · LiteLLM).
- [`engine/nodes.py`](../../engine/nodes.py) — `frame_question` · `generate` · `evaluate` · `refine`.
- [`engine/graph.py`](../../engine/graph.py) — `_build_retriever` (which middle node a pack gets).
