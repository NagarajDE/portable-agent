# Skill: time intelligence
- State grain and timezone explicitly; "last month" is ambiguous across zones and
  fiscal vs calendar calendars.
- Comparisons: MoM / QoQ / YoY — YoY absorbs seasonality, MoM catches momentum;
  pick deliberately and label it.
- Partial periods: flag month-to-date vs full month; don't compare a partial
  current period to a complete prior one.
- Watch calendar effects: differing days-per-month, business-day counts,
  holidays, DST, and leap years distort naive period-over-period deltas.
