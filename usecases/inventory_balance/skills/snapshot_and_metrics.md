# Skill: inventory snapshot & metrics (how to READ the evidence)
# The SQL tool (Cortex Analyst) runs the query from the semantic model BEFORE you answer. These
# rules govern how you INTERPRET the returned evidence and what you tell the user — they do NOT
# rewrite the query. Query-level semantics (e.g. a default snapshot filter) live in the semantic
# model, not in this skill.
- Each row is a point-in-time position identified by SnapshotDate (a fiscal month-end close).
- "Current" questions expect the most-recent snapshot. If the evidence carries a MostRecentSnapshot flag or a single latest SnapshotDate, treat it as current state. If it spans MULTIPLE SnapshotDates, do NOT sum across them (that double-counts) — report per snapshot, or say the current snapshot is what's needed.
- Coverage: Weeks On Hand (external) is the default coverage metric; flag materials below safety-stock coverage when the evidence shows it.
- Segment by ProductType (Consumables / Instruments / Spares / Other) when the field is present — the categories are mutually exclusive.
- Savings categories (excess, expired, blocked, parameter) can overlap — do NOT sum them without dedup; say so if a deduplicated total isn't computable from the evidence.
- Only report values present in the evidence; if a question needs data that isn't there, say so explicitly rather than estimate.
