# Skill: how to DECOMPOSE an excess-disposition question (for the PLANNER)

Answering "what to do with our excess" needs a CHAIN, not one query — rank the excess first, then look
up forward demand for exactly those materials, then classify. Plan it as these steps:

- **s1 — rank excess by value.** Ask for the top excess positions by excess value ($) descending, at the
  grain Material x Plant x Storage Location. Include for each: material, plant, storage location, MRP
  controller, excess quantity (current stock − max inventory level), max inventory level, and
  TOTAL_EXCESS_STOCK_VALUE. Use the most recent snapshot. Return the top ~10.
- **s2 — forward demand for THOSE materials** (depends on s1). Ask ONLY for the quarter-end forecast
  demand (FORECAST_DEMAND_QTY_QTR_END_SUM) per material for the materials from s1. Reference them with
  `{{s1}}`, and phrase it as a plain **comma-separated list of material numbers** ("... for materials
  20007746, 20141208, ...") — carry only the material identifiers forward, never the whole s1 table.
- **s3 — cross-plant demand** (optional; depends on s1). If redistribution is in scope, ask in a
  SEPARATE step for cross-plant demand / other plants holding or needing those materials.

Keep each step to ONE measure. A single step that asks for forecast demand AND cross-plant demand at
once is markedly more likely to come back EMPTY from the text-to-SQL tool — split them (s2, s3) rather
than combining. If a demand step does come back empty, that's a query-coverage gap, not a data gap: the
answer step should classify on excess vs. max-level and say demand wasn't retrieved (never fabricate it).

Keep it to the minimum steps that answer the question. Do not add filters the user didn't ask for.
Prefer a SCOPED population (a specific plant and the top-N by value) over "all plants" — a broad,
unscoped result is slow to synthesize and rarely what the human needs first; if the question is broad,
still rank to the top-N so the answer stays focused and fast.
Finished-good instruments (ProductType = 'Instrument' AND MaterialPlanningFamily = 'INS') have limited
post-manufacturing actionability — the answer step notes them; you need not special-case them in the plan.
