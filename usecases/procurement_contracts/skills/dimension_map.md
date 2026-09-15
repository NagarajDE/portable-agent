# Optional note: which column a value lives in (query-framing hint)
OPTIONAL developer hint for the FIRST-try framing -- it says which COLUMN a kind of value lives in. Any
values below are EXAMPLES for reference, not an authoritative or exhaustive list: the live data is the
source of truth, and if a first-try query comes back empty the real current values are looked up
automatically. A stale example here is harmless.

## Contract status -> BUSINESS_STATUS
- A status word the user says ("active", "live", "expired") maps to the **BUSINESS_STATUS** column. FYI:
  "active"/"live" typically means the in-force signed states (e.g. Executed / Approved) and "expired" the
  lapsed one -- treat these as examples and use whatever the column actually stores.

## Other named values -> their column
- **Contract value** measure = **UPDATED_CONTRACT_VALUE** when present, else **CONTRACT_VALUE**.
- **Supplier / vendor** -> **SUPPLIER_NAME** (use **PARENT_SUPPLIER_NAME** to roll up subsidiaries).
- **Sourcing category** -> **SOURCING_CATEGORY** (sub-level **SOURCING_SUBCATEGORY**).
- **Geography** -> **GEO_COUNTRY** / **GEO_REGION**; a contract code -> **AGREEMENT_CODE**.
- Dates: **EFFECTIVE_DATE** / **EXPIRY_DATE** (use the fiscal EFFECTIVE_*/EXPIRY_* fields for period analysis).
