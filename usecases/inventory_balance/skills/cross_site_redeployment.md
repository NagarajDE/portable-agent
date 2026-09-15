# Skill: cross-site redeployment
Mirrors the native `cross_site_redeployment` skill. Use to rebalance inventory across plants -- transfer
excess at one site to cover shortage at another. Current state (`MostRecentSnapshot = TRUE`); grain
Material x Plant.

- Per material x plant: Excess = On-Hand - Target (where > 0); Shortage = Target - On-Hand (where > 0,
  Target = Max Level or safety stock). Find materials with BOTH an excess plant (source) and a shortage
  plant (destination). Rank matched pairs by the smaller of (source excess $, destination shortage $) --
  the actionable transfer value. Transfer Qty = MIN(source excess, destination need) -- never more.
- EXCLUDE finished-good instruments (`ProductType='Instruments' AND MaterialPlanningFamily='INS'`) and
  field-stock plants from the matching.
- Priority: Critical (destination WOH < 1 week, no inbound within lead time) > High (transfer cancels an
  open buy) > Medium (rebalances over/under-target) > Low (marginal; freight may exceed benefit).
- Optionally validate with forward MRP (CurrentFiscalQuarterIndicator IN (0,1)) and 13-week consumption.
