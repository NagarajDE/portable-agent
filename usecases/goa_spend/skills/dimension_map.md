# Optional note: which column a value lives in (query-framing hint)
OPTIONAL developer hint for the FIRST-try framing -- which COLUMN a named value lives in, so a filter is
not guessed onto the wrong column. Any specific values below are EXAMPLES, not authoritative or exhaustive:
the live data is the source of truth, and an empty first-try query triggers an automatic lookup of the real
current values. A stale example here is harmless.

## Category NAME -> ExecutiveCategory
- A category name the user mentions (e.g. "IT Software", "MRO", "Marketing") is a value of the
  **ExecutiveCategory** dimension (the L1 executive category). Bind "by category" / "for the X category"
  to **ExecutiveCategory**, NOT to SpendCategory or any other column -- match the user's wording to the real
  ExecutiveCategory value (looked up automatically if the first try comes back empty). Sub-levels are
  **L2Category** / **L3Category**. Rows with no ExecutiveCategory are uncategorized Non-PO / Concur T&E spend.
- SpendCategory is a separate, coarser classification; do NOT filter a named executive category against it.

## Other named values -> their column
- **Cost center** (e.g. "100.4321" or "1004321") -> **CostCenter** (normalize dots); GL account -> **GLAccount**
  / the L01_AcctCategoryName..L05_AcctDivisionName hierarchy.
- **Region** ("EMEA", "APAC", "AMR") -> **BudgetOwnerRegionName** (or **PORegionCode**); a country/company
  -> **CompanyCode**.
- **Vendor / supplier** -> **ParentVendorName** (rolls up subsidiaries); vendor tier -> VendorCategory.
- **PO vs Non-PO** -> **SpendType**; **Concur / T&E / card** spend -> **Source**.

## Measure & scope
- The money measure is **InvoiceUSD** (signed; no ABS). Use it for spend totals/averages unless the user
  asks for local currency -- then use **InvoiceAmount** with **DocumentCurrency**.
- Use fiscal date fields (FiscalYear_BVDATE, FiscalQuarter, FiscalMonth) for time, not calendar, unless the
  user says calendar.
- "Categorized / sourcing-actionable" = ExecutiveCategory IS NOT NULL; "all indirect spend" also includes
  uncategorized Non-PO / Concur. Do not invent this scope during framing -- only bind an explicitly named one.
