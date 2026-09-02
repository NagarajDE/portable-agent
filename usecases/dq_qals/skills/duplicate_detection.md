# Skill: duplicate detection (practical)
- Define the business key first. In SAP QM, an inspection lot is keyed by
  PRUEFLOS; a "duplicate" is >1 physical row for one PRUEFLOS.
- Composite keys: concatenate with a non-data delimiter and COALESCE NULLs first,
  else NULLs collide or split groups:
  `MD5(COALESCE(a,'')||'|'||COALESCE(b,''))`.
- Snowflake `HASH(*)` for full-row dupes; SAP HANA `HASH_SHA256` over explicit
  columns (order matters).
- Report three states per key: DUPLICATE (n>1), UNIQUE (n=1), MISSING (n=0) — the
  last catches expected keys that never arrived.
