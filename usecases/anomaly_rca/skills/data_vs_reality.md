# Skill: rule out data artifacts before business causes
- Check freshness and load completeness first: a "drop" is often a late, partial,
  or failed load, not a real decline. Compare row counts vs the prior period.
- Look for pipeline changes: backfills, dedup gaps, schema or logic changes,
  timezone shifts, and metric-definition changes in the window.
- Only after artifacts are excluded should you attribute the change to real
  business or external factors — and say which you ruled out.
