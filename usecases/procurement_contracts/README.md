# procurement_contracts — iCertis contract analytics

Mirrors native `DBADMIN.AGENTS.PROCUREMENT_CONTRACT_AGENT` (read-only reference). That native agent
ships no custom instructions or sample questions, so the skills/rubric here are authored from its
semantic view's column set (contract lifecycle, value, risk, supplier).

- Semantic view (live): `DB_ENTERPRISE_DW_VAL.GSC_PROCUREMENT.ICERTIS_CONTRACTS`
  (underlying `BLV_ICERTIS_CONTRACT`); declared in `semantic_layer.yaml`.
