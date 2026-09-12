# Skill: indirect-spend metrics, scope & reporting
- Default metric is InvoiceUSD, and it is signed — do NOT apply ABS unless the user asks for gross/absolute totals.
- Use fiscal time fields (fiscal year/quarter/month) by default; use calendar time only when the question asks for it.
- SCOPE matters — state which spend you are reporting:
  - "Categorized / sourcing-actionable" = ExecutiveCategory is present (PO-backed, Ariba-enriched).
  - "All indirect spend" also includes Non-PO / Concur T&E records that carry no category.
  - If the intent is sourcing/savings/category strategy, default to categorized and say so; if the intent is total financial exposure/budget/audit, use all spend and say so; when unclear, state the assumption.
- Lead with the number, be concise; explain a field's business meaning only when it aids understanding.
- For rankings, give the top-K with each item's share of the total (%). Format money as $ with commas, or $M for large sums.
- Only report values present in the DATA; never infer violations, risk scores, or supplier ratings that the data does not contain.
