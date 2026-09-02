"""Demo-only data for the parity use case. Bypassed entirely when SQL_TOOL != mock."""

class MockSQLTool:
    def ask(self, question: str) -> str:
        return ("TABLE | HANA_COUNT | SNOWFLAKE_COUNT | MATCH\n"
                "MARA  | 1000000    | 1000000         | YES\n"
                "MARC  | 4500000    | 4499998         | NO\n"
                "MBEW  | 2300000    | 2300000         | YES")

CANNED = {
    "draft":   "Some tables match between HANA and Snowflake and one does not.",
    "refined": ("MARC is out of parity: SAP HANA has 4,500,000 rows vs Snowflake "
                "4,499,998 — 2 rows missing on Snowflake. MARA and MBEW match exactly."),
}
