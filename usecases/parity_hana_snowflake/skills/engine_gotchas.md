# Skill: engine gotchas
- Trim trailing spaces and normalize case before counting (avoids false mismatches).
- Normalize HANA vs Snowflake NULL handling before comparing.
- Late-arriving CDC can cause transient off-by-small-N deltas; note if suspected.
