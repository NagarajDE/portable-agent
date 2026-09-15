# Skill: Non-PO & Concur spend
Mirrors the native `non_po_concur_spend` skill. Use for Non-PO spend, Concur, credit/P-Card/Corporate Card,
T&E, or out-of-pocket reimbursements.

- Spend types: PO (`SpendType='PO'`, `Source='SAP'` -- full compliance fields); SAP Non-PO
  (`SpendType='NonPO'`, `Source='SAP'`); Concur (`Source='CONCUR'`).
- Non-PO / Concur have NO PO number, requestor, or compliance flags, and ExecutiveCategory is typically
  NULL -- but CostCenter and BudgetOwner ARE available. State these limitations when answering.
- Payment type (Corporate Card, P-Card, Event Card, Out of Pocket) is DIFFERENT from expense category
  (ConcurExpenseCategory / ConcurExpenseType = WHAT was bought: airfare, hotel, meals). Don't conflate them.
- For month-specific Concur questions, use explicit FiscalMonth ranges rather than YTD indicators.
