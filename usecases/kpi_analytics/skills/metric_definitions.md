# Skill: pin the metric definition first
- Agree the definition before computing: net vs gross, bookings vs billings vs
  recognized, gross vs net of returns/discounts.
- Additivity:
  - additive (revenue, units) — safe to SUM across any dimension.
  - non-additive (rates, %, AOV, distinct users) — must be RECOMPUTED at the
    target grain, never summed/averaged of averages.
  - semi-additive (balances, inventory on hand) — sum across entities but take
    the last value in a period, not the sum, across time.
- Fan-out guard: a JOIN to a one-to-many table multiplies the measure. Aggregate
  the fact FIRST, then join, or COUNT(DISTINCT).
