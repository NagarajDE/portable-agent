# Optional note: which column a value lives in (query-framing hint)
OPTIONAL developer hint for the FIRST-try framing -- which COLUMN a named value lives in. Any specific
values below are EXAMPLES, not authoritative or exhaustive: the live data is the source of truth, and an
empty first-try query triggers an automatic lookup of the real current values. A stale example here is
harmless.

## Named values -> their column
- **Product type** (e.g. "Consumables", "Instrument", "Spares") -> the **ProductType** dimension
  (mutually exclusive; every material is exactly one). Bind a product-type filter to ProductType, and match
  the user's wording to the real stored value (looked up automatically if the first try comes back empty --
  the user's plural/singular may differ from what is stored).
- **Plant** -> Plant Code (+ Plant Name); **material** -> Material Number (+ Material Description);
  storage location -> Storage Location.
- Forward supply/demand lives in **MRP_STOCK_TRANSACTIONS**; **MRPElementCategory** separates supply vs
  demand transactions.

## Measure
- Inventory value is a dollar measure (calculated plant stock value); quantities are unit measures -- pick
  the one the user asked for. **Weeks On Hand (WOH, external)** is the default coverage metric.

## Scope (do NOT inject during framing)
- "Current" state corresponds to `MostRecentSnapshot = TRUE` and historical/trend analysis omits it and
  groups by SnapshotDate / fiscal period -- but that current-vs-historical CHOICE is a scope default made
  by the semantic model and the answer step, NOT by framing. Framing must not add a snapshot filter; only
  bind an explicitly named product type / plant / material and the measure.
