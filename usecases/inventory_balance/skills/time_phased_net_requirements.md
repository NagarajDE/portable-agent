# Skill: time-phased net requirements
Mirrors the native `time_phased_net_requirements` skill. Use for forward coverage, projected stockouts, or
a time-bucketed supply/demand view. Starting inventory from current snapshot + forward MRP_STOCK_TRANSACTIONS.

- Build a fiscal-period grid per material x plant: Beginning Stock -> + Supply (In) - Demand (Out) ->
  Ending Stock -> Coverage (WOH). Period 1 beginning = current on-hand (`MostRecentSnapshot = TRUE`);
  each later period begins at the prior period's ending stock. Default horizon: current fiscal year.
- Supply (In) = supply-category MRP transactions in the period; Demand (Out) = demand-category. Use fiscal
  month (FiscalMonthNumber) buckets; fiscal quarter for a higher-level view.
- Flag critical periods: Ending Stock < safety stock (coverage gap); <= 0 (projected stockout); demand
  spike; or supply concentration. If > 50% of a critical period's supply is unfirmed (planned orders, not
  confirmed POs), flag "supply fragility -- coverage depends on planned orders converting".
