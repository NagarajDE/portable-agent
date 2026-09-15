# Skill: time comparison (YoY / QoQ / MoM / trend)
Mirrors the native `time_comparison` skill. Use for period-over-period comparisons, trends, growth rates,
or CAGR.

- ALWAYS use fiscal periods (FiscalYear_BVDATE, FiscalQuarter, FiscalMonth in YYYYMM) -- Illumina's fiscal
  year does not start January 1. Only use calendar periods if the user explicitly asks.
- Prior-year month = current month - 100 (e.g. 202601 -> 202501).
- Compare COMPLETED periods only, unless the user explicitly asks for partial / YTD.
- Completeness: when the user asks for a trend across N periods, return a row for EVERY one of those N
  periods (don't silently drop periods with no spend).
- Flag anomalies: > 20% QoQ or > 15% YoY change; name the top driver (vendor or category) and note whether
  a drop may be a timing shift rather than a real reduction. CAGR = (end/start)^(1/years) - 1.
