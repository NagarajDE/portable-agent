# Skill: SQL safety
- Read-only: SELECT queries only. Never write, update, delete, or run DDL.
- Prefer explicit column lists and business keys over SELECT *.
- Handle NULLs explicitly (COALESCE) before comparisons or hashing.
