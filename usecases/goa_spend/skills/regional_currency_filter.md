# Skill: regional & currency filter
Mirrors the native `regional_currency_filter` skill. Use for spend filtered by region, company code, or
country, or when local-currency display is requested.

- Region: "EMEA / APAC / AMR" -> **BudgetOwnerRegionName** or **PORegionCode** (they can give different
  splits -- always state which field you used); a named country/entity -> **CompanyCode**. Some rows have
  NULL PORegionCode (Other/Unassigned).
- Currency: default **InvoiceUSD** (always USD). For local currency use **InvoiceAmount** with
  **DocumentCurrency**, and NEVER sum across different DocumentCurrency values without normalizing to USD.
  Always note when a figure is shown in local currency.
