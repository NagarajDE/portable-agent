# Skill: the six data-quality dimensions (and how to test each in SQL)
- Completeness — required fields populated.
  `COUNT(*) FILTER (WHERE col IS NULL) / COUNT(*)` = null rate per column.
- Uniqueness — no unintended duplicates on the business key.
  `GROUP BY key HAVING COUNT(*) > 1`; use the FULL composite key.
- Validity — values in the allowed domain / range / format.
  range checks, `REGEXP_LIKE`, or `NOT IN (SELECT code FROM ref_domain)`.
- Consistency — related fields/tables agree.
  cross-field (`end_date >= start_date`), cross-table (totals reconcile).
- Referential integrity — no orphans.
  `child LEFT JOIN parent ... WHERE parent.key IS NULL` (anti-join).
- Timeliness — data is fresh vs its SLA.
  `MAX(load_ts)` vs expected; row-count delta vs prior load.
