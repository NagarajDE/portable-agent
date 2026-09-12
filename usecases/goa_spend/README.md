# goa_spend — indirect spend analytics

Mirrors the native `DBADMIN.AGENTS.GOA_SPEND_ANALYTICS_AGENT` (read-only reference; not modified).

- Semantic view (live): `DB_ENTERPRISE_DW_VAL.GSC_PROCUREMENT.INDIRECT_SPEND` (declared in `semantic_layer.yaml`)
- House rules distilled into `skills/spend_rules.md` (InvoiceUSD default, fiscal periods, categorized
  vs all-spend scope) and `prompts/rubric.md`.

Live run: the semantic view is declared in `semantic_layer.yaml`, so just set `SQL_TOOL=cortex`
(+ Snowflake creds) and `python run_local.py goa_spend`. Mock run (no creds): `python run_local.py goa_spend`.
