# Skill: cost center & GL account lookup
Mirrors the native `cost_center_gl_lookup` skill. Use for spend by cost center, GL account, budget owner,
or account hierarchy.

- Cost center fields: **CostCenter** (filter on this), CostCenterName / CostCenterLabel (display),
  CostCenterCategoryName. Normalize dot notation -- "100.4321" and "1004321" are the same; try both.
- Budget owner: BudgetOwnerName, BudgetOwnerLoginID, BudgetOwnerRegionName, BudgetOwnerL2Manager..L8Manager.
- GL hierarchy (5 levels): L01_AcctCategoryName > L02_AcctSubCategoryName > L03_AcctGroupName >
  L04_AcctSegmentName > L05_AcctDivisionName; direct fields GLAccount / GLAccountName / GLAccountLabel.
- Known gap: subscription vendors (e.g. IEEE, Wiley) may sit under different GL codes -- when a user asks
  for "IT Software and Subscriptions", include both the category AND the relevant GL accounts.
