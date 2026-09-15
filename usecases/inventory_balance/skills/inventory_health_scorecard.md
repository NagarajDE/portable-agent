# Skill: inventory health scorecard
Mirrors the native `inventory_health_scorecard` skill. Use for an overall health assessment / KPI summary /
maturity evaluation. Current state (`MostRecentSnapshot = TRUE`); default scope by L2 manager across plants.

- FIRST check data availability: only score dimensions whose required columns/measures exist in the model;
  mark others "Not Scored -- required data not available" and reweight the composite to scored dimensions
  only. Never impute missing metrics.
- Score six Gartner-aligned dimensions 1-5 with Red/Amber/Green:
  1. Stock Efficiency (avg WOH vs target, excess %, turns), 2. Service Risk (stockout exposure, A-items
  below safety stock, days-to-stockout), 3. Working Capital Productivity (active cycle stock % vs
  non-productive: blocked + expired + no-movement-13-weeks), 4. Parameter Health (safety stock / ROP /
  lot-size adequacy), 5-6 as data supports.
- Use fiscal month-end snapshots for trend context. Present the composite plus per-dimension ratings and
  the biggest gaps by dollar impact.
