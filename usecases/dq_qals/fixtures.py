"""Demo-only data for the DQ/QALS use case. Bypassed when SQL_TOOL != mock."""

class MockSQLTool:
    def ask(self, question: str) -> str:
        return ("INSPECTION_LOT | ROWS\n"
                "40001122       | 2\n"
                "40001133       | 2\n"
                "40001140       | 2\n"
                "(total lots today: 12,480)")

CANNED = {
    "draft":   "The data shows some duplicate lots in QALS.",
    "refined": ("3 inspection lots in QALS are duplicated today (40001122, 40001133, "
                "40001140), each appearing twice — 6 excess rows of 12,480 (0.05%). "
                "Likely a re-post in the ECC->bronze load; fix by deduping on PRUEFLOS "
                "in staging."),
}
