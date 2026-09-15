# Skill: zero demand / no-movement candidates
Mirrors the native `zero_demand_no_movement` skill. Use for dead/dormant stock, slow movers, no-movement
analysis. Current state (`MostRecentSnapshot = TRUE`).

- Identify positions with inventory value > $0 AND zero consumption (goods issues) in the trailing 13
  weeks AND zero forward demand (no demand-category MRP transactions for CurrentFiscalQuarterIndicator IN
  (0,1)). Rank top 25 by total inventory value.
- EXCLUDE finished-good instruments (`ProductType = 'Instruments' AND MaterialPlanningFamily = 'INS'`).
- Enrich each with last consumption date, days since last movement, lifecycle status (active / phase-out /
  obsolete / blocked), stock-type split, and shelf life/expiry.
- Disposition by picture: obsolete -> E&O; >52 weeks dormant + active status -> obsolescence review;
  26-52 weeks + no network demand -> disposition review; 13-26 weeks + active elsewhere -> redistribute;
  blocked -> resolve QA hold; near-expiry (<=90 days) -> expedite disposition.
