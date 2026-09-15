# Skill: top-N ranking
Mirrors the native `top_n_ranking` skill. Use for ranked lists -- top vendors, categories, cost centers,
budget owners, GL accounts ("who/what are the biggest").

- Default N = 10 when unspecified. Vendors rank by **ParentVendorName** (rolls up subsidiaries) unless the
  user asks for the raw VendorName -- state which you used.
- Scope: "sourcing-actionable" = `ExecutiveCategory IS NOT NULL` (categorized PO spend). If the user says
  "top vendors" without qualifying scope, ASK (categorized vs all indirect) -- it changes the ranking.
- Enrich: show each item's % of total and the cumulative %, and include the total denominator so the
  percentages are meaningful. Flag concentration risk (any single vendor > 25%, or top-3 > 50%).
