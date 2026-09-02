# Skill: slicing & filters
- Always state the filters applied and the default exclusions (test accounts,
  voided/soft-deleted rows, internal orgs).
- Beware NULLs in filter predicates: `WHERE region <> 'EU'` silently drops NULL
  regions; use `IS DISTINCT FROM` or COALESCE when NULLs should count.
- Simpson's paradox: an aggregate trend can reverse within every segment. When a
  total surprises, break it down before concluding.
