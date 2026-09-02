# Skill: duplicate detection
- Snowflake: GROUP BY the business key (or HASH(*)) and flag COUNT(*) > 1.
- SAP HANA: HASH_SHA256 over explicit columns, concatenated with a delimiter,
  COALESCE nulls first so blanks don't collide.
- Report three states per key: YES (duplicate), NO (unique), EMPTY (no rows).
