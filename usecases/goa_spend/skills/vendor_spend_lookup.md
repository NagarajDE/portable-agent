# Skill: vendor spend lookup
Mirrors the native `vendor_spend_lookup` skill. Use for spend with a specific vendor/supplier/parent
company, including subsidiary rollups and multi-year history.

- Resolve identity to **ParentVendorName** first (this rolls up subsidiaries -- e.g. "IBM, IBM Canada,
  Red Hat"); fall back to VendorName ILIKE, or ResolvedVendorNumber for a vendor code. ALWAYS total by
  ParentVendorName so subsidiary spend isn't missed.
- Default time scope: current fiscal year YTD (FiscalYear_BVDATE = current AND FiscalYearToDateIndicator = 1);
  "last 3 years" -> current, current-1, current-2. Fiscal unless the user says calendar.
- Clarify spend scope when not obvious: categorized (ExecutiveCategory IS NOT NULL) vs all indirect; PO vs
  all spend types. Enrich with VendorCategory tier (Strategic/Maintain/Develop/Probation/Exit).
- If an exact vendor match fails, show the closest matches and ask the user to confirm rather than guessing.
