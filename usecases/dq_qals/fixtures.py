"""Demo-only data for the DQ/QALS use case. Bypassed entirely when SQL_TOOL != mock."""

class MockSQLTool:
    def ask(self, question: str) -> str:
        return ("INSPECTION_LOT | COUNT\n"
                "40001122       | 2\n"
                "40001133       | 2\n"
                "40001140       | 2")

# Text the MOCK LLM fakes (real providers ignore this).
CANNED = {
    "draft":   "The data shows duplicate lots in QALS.",
    "refined": ("There are 3 duplicate inspection lots in QALS today "
                "(40001122, 40001133, 40001140), each appearing twice."),
}
