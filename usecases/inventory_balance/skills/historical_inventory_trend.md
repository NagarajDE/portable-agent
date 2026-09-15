# Skill: historical inventory trend
Mirrors the native `historical_inventory_trend` skill. Use for how inventory changed over time,
period-over-period comparisons, or trend analysis.

- Use historical snapshots -- do NOT apply `MostRecentSnapshot = TRUE`. Group by SnapshotDate,
  FiscalMonthCode, FiscalQuarterName, or FiscalYearNumber. Each data point is a fiscal month-end position.
  Default period: last 4 fiscal quarters; default granularity: quarter (month for detail).
- Per period, compute the requested metric(s) (total value, excess value/%, avg WOH, below-safety-stock
  count, non-productive %) with period-over-period change (absolute, %, and a direction indicator).
- NEVER SUM across snapshots (that double-counts) -- use snapshot-level values and compare/trend them.
- For a > 10% shift, drill into which plants / material groups / product types drove it; distinguish a
  structural shift from a cyclical pattern. Recommend a line chart for 3+ periods.
