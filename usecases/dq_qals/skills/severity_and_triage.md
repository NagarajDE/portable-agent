# Skill: severity & triage
- Always report BOTH absolute count and rate; a rate alone hides scale, a count
  alone hides proportion.
- Compare to a baseline: prior run, same weekday, or a trailing average — a
  number is only anomalous relative to normal.
- Classify: BLOCKING (breaks downstream / violates a hard contract) vs WARNING
  (degraded but usable). Say which.
- Localize before escalating: which table, column, source system, and load
  window. "Bad data" is not actionable; "MARC.WERKS null for 2.5% of rows loaded
  after 02:00" is.
