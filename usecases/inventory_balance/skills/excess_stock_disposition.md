# Skill: excess stock disposition
Mirrors the native `excess_stock_disposition` skill. Use for top excess inventory by value and the
recommended disposition path. Current state (`MostRecentSnapshot = TRUE`).

- Rank positions where current unrestricted stock exceeds the suggested **Max Inventory Level**, by excess
  value ($) descending. Excess Value = (Current Stock Qty - Max Level) x Standard Cost. Grain defaults to
  Material x Plant x Storage Location -- state it.
- EXCLUDE finished-good instruments (`ProductType = 'Instruments' AND MaterialPlanningFamily = 'INS'`) --
  high value but limited post-manufacturing actionability; note the exclusion.
- Assess forward demand/supply from MRP_STOCK_TRANSACTIONS (CurrentFiscalQuarterIndicator IN (0,1)) to
  classify each: Self-Correcting (demand will consume it) / Partially Absorbed / Stagnant.
- Disposition path by outlook: Self-Correcting -> Hold; Partially Absorbed -> reduce/defer inbound supply;
  Stagnant + demand elsewhere -> redistribute (STO); Stagnant + no demand -> disposition review;
  expired/near-expiry -> E&O process. Persistent excess (across snapshots) is higher priority.
