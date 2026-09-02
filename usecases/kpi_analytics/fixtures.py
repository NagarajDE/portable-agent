"""Demo-only data for the KPI analytics use case. Bypassed when SQL_TOOL != mock."""

class MockSQLTool:
    def ask(self, question: str) -> str:
        return ("MONTH   | NET_REVENUE\n"
                "prior   | 3,880,000\n"
                "last    | 4,200,000")

CANNED = {
    "draft":   "Revenue went up last month.",
    "refined": ("Net revenue last month was $4.20M, up 8.3% MoM from $3.88M. "
                "Definition: net of returns and voided orders; full calendar month, "
                "UTC. Trend is positive but check for month-length effects."),
}
