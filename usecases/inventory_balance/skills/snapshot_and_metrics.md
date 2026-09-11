# Skill: inventory snapshot & metrics
- Current-state is the DEFAULT: filter MostRecentSnapshot = TRUE for any "current" balance, coverage, or risk question.
- Historical/trend questions omit that filter and group by fiscal period — each snapshot is a fiscal month-end position; never SUM across snapshots (it double-counts).
- Coverage metric: default to Weeks On Hand (external); flag materials below safety-stock coverage.
- Segment by ProductType (Consumables / Instruments / Spares / Other) — the categories are mutually exclusive.
- Savings categories (excess, expired, blocked, parameter) can overlap — do NOT sum them without dedup; say so if a deduplicated total isn't computable.
- Only report values present in the data; if a question needs data not in the model, say so explicitly rather than estimate.
