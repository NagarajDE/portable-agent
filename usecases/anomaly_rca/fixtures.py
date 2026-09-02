"""Demo-only data for the anomaly RCA use case. Bypassed when SQL_TOOL != mock."""

class MockSQLTool:
    def ask(self, question: str) -> str:
        return ("CHANNEL | ORDERS_YDAY | ORDERS_PRIOR | DOD\n"
                "web     | 8,100       | 8,050        | +0.6%\n"
                "mobile  | 1,300       | 5,900        | -78%\n"
                "(freshness OK; total row counts normal)")

CANNED = {
    "draft":   "Orders dropped yesterday for some reason.",
    "refined": ("The 30% order drop is isolated to the mobile channel (down 78%), "
                "while web was flat (+0.6%) — consistent with a mobile checkout "
                "outage. Freshness and row counts are normal, so this is a real drop, "
                "not a pipeline artifact. Confidence: high. Next: confirm the mobile "
                "API error window."),
}
