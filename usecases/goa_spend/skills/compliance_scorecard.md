# Skill: compliance & governance scorecard
Mirrors the native `compliance_scorecard` skill. Use for procurement policy compliance -- preferred-supplier
usage, catalog adoption, after-the-fact (ATF) spend, bid waivers, maverick buying, governance audits.

- SCOPE RULE (read first): every compliance metric is defined ONLY over PO spend (`SpendType = 'PO'`).
  Non-PO and Concur bypass the requisition process, so their compliance fields are blank; including them
  silently deflates every rate. Always apply the PO-only scope and state it.
- Four core rates, all weighted by InvoiceUSD (not transaction counts):
  - Preferred supplier % (`PreferredSupplierFlag = 'Y'`) -- higher is better.
  - Catalog % (`CatalogOrNonCatalog = 'Catalog'`) -- higher is better.
  - After-the-fact % (`ATFSpendIndicator = 'Y'`, invoiced before a PO existed) -- lower is better.
  - Bid-waiver % (`BidWaiverFlag = 'Y'`, competitive bidding waived) -- lower is better.
- Maverick buying = non-catalog AND non-preferred together. Never present the four rates as if they point
  the same direction. Bid waivers can be broken out by `BidWaiverReasonCode` with a materiality threshold.
