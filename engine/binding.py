"""
ENGINE / binding -- TERM -> STORED-VALUE binding without hand-authoring every column.

The class of bug: the user (or an exemplar) says `status = 'Active'` but the column stores
`Executed`/`Approved` -> 0 rows -> no_data. A wide view (100-200 columns) can't have every column's
values hand-authored, so the mechanism is HYBRID:
  * a pack may hand-declare a FEW high-value/ambiguous dims (`recover_value_dims`) and skills;
  * the rest is AUTO-DERIVED here from the query the tool actually ran: on an empty result we extract the
    `col = 'literal'` / `col IN (...)` predicates, resolve each column to a dimension of the semantic
    view, look up its REAL distinct values, and hand those to the reformulate step (no pre-enumeration);
  * optionally a LOW-cardinality VALUE CATALOG (`value_catalog: {max_cardinality: N}`) is profiled once
    and injected into FRAMING so the first try binds correctly; high-cardinality dims (names, ids) are
    skipped automatically.
Pure functions only; the adapter (CortexAnalystTool) supplies `last_sql`, `dimension_paths()`, and
`distinct_values()`; a tool without them (mock) simply degrades to skills-only binding.
"""
from __future__ import annotations

import re

_STR = r"'(?:[^']|'')*'"                                        # a SQL string literal ('' escaped)
_IDENT = r"(?:[A-Za-z_][\w$]*\.)*[A-Za-z_][\w$]*|\"[^\"]+\""     # dotted or quoted identifier
_EQ = re.compile(rf"({_IDENT})\s*(?:=|<>|!=)\s*{_STR}", re.IGNORECASE)
_IN = re.compile(rf"({_IDENT})\s+(?:NOT\s+)?IN\s*\(\s*{_STR}(?:\s*,\s*{_STR})*\s*\)", re.IGNORECASE)


def _bare(ident: str) -> str:
    """Last dotted segment, quotes stripped, upper-cased: `t."Business Status"` -> `BUSINESS STATUS`."""
    seg = ident.split(".")[-1]
    return seg.strip('"').strip().upper()


def filter_columns(sql: str) -> list[str]:
    """Columns compared to STRING literals in equality / IN predicates -- the ones a wrong value would
    have made match nothing. Order-preserving, de-duplicated, upper-cased bare names. Conservative:
    numeric comparisons, LIKE, and ranges are ignored (a number rarely needs term binding)."""
    out: list[str] = []
    for pat in (_EQ, _IN):
        for m in pat.finditer(sql or ""):
            col = _bare(m.group(1))
            if col and col not in out and col.upper() not in ("AND", "OR", "NOT"):
                out.append(col)
    return out


def resolve_dims(columns: list[str], dim_paths: list[str]) -> list[str]:
    """Map bare column names to the semantic view's `TABLE.DIM` paths by last-segment match
    (case-insensitive). Unknown columns are dropped; order follows `columns`."""
    by_name: dict[str, list[str]] = {}
    for p in dim_paths or []:
        by_name.setdefault(_bare(p), []).append(p)
    out: list[str] = []
    for c in columns:
        for p in by_name.get(c.upper(), []):
            if p not in out:
                out.append(p)
    return out


def render_values_block(found: dict, *, header: str) -> str:
    """`{path: [values]}` -> a compact prompt block ('' when empty). Values are shown as-is (they are the
    authoritative stored spellings); the path is reduced to its dimension name for readability."""
    lines = [f"- {p.split('.')[-1]}: " + ", ".join(vals) for p, vals in (found or {}).items() if vals]
    return (header + "\n" + "\n".join(lines)) if lines else ""


RECOVERY_HEADER = ("STORED VALUES (the real values currently in these columns; the user's word may differ -- "
                   "plural/singular, code vs label -- so map it to the EXACT value shown):")
CATALOG_HEADER = ("STORED VALUES (authoritative -- the real values currently in these low-cardinality columns; "
                  "when the user names one of these, bind to the EXACT stored spelling, never a guess):")
