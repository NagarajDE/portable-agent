# Skill: supply transaction cancellation / deferral
Mirrors the native `supply_transaction_cancellation` skill. Use to reduce inbound supply -- cancel/defer/
resize open POs and planned orders to prevent future excess. Current state inventory + forward MRP.

- Project excess per position over the horizon (CurrentFiscalQuarterIndicator IN (0,1)):
  Projected Inventory = Current On-Hand + Inbound Supply - Outbound Demand; Projected Excess =
  Projected Inventory - Target (Max Level, or safety stock + cycle stock). Rank top positions by projected
  excess value. EXCLUDE finished-good instruments (`ProductType='Instruments' AND MaterialPlanningFamily='INS'`).
- For each open supply-category MRP transaction (order number/line, MRPElement, qty, planned date, value),
  recommend an action: **Cancel** (qty <= projected excess, no near-term demand, still changeable),
  **Defer** (demand exists but back-loaded / delivery early), **Resize** (partial surplus -- reduce to the
  gap), or **Hold** (needed within lead time or past the change window). State the savings per action.
