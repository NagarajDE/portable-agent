# Skill: procurement contract analytics (iCertis contracts)
- Grain is one row per contract (AGREEMENT_CODE), with a SUPPLIER_NAME, CONTRACT_VALUE (and UPDATED_CONTRACT_VALUE), EFFECTIVE_DATE / EXPIRY_DATE, and risk fields (LEGAL_RISK, IP_RISK, DATA_ACCESS_RISK, INSURANCE_RISK, SUPPLIER_RISK).
- Contract value: prefer UPDATED_CONTRACT_VALUE when present (reflects amendments), else CONTRACT_VALUE; format money as $ with commas or $M.
- Use the fiscal date fields (EFFECTIVE_*/EXPIRY_* fiscal year/quarter/month) for time analysis; report dates as YYYY-MM-DD.
- Lifecycle: use BUSINESS_STATUS for active vs expired; renewals via IS_RENEWAL / RENEWAL_PERIOD / MAX_RENEWALS; bucket upcoming expirations by 30 / 60 / 90 days.
- Segment by SOURCING_CATEGORY, GEO_COUNTRY / GEO_REGION, or PURCHASING_ORG when relevant.
- Only report values present in the DATA; flag risk/compliance findings as action items, and never infer a risk rating the data does not carry.
