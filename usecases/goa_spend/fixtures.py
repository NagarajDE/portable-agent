"""Demo-only mock data for goa_spend (mirrors GOA_SPEND_ANALYTICS_AGENT). Bypassed on live Cortex."""

class MockSQLTool:
    def ask(self, question: str) -> str:
        return ("VENDOR     | SPEND_USD\n"
                "Acme Labs  | 4200000\n"
                "Globex     | 3100000\n"
                "Initech    | 2600000\n"
                "(top 3 vendors; total categorized spend 9.9M USD)")

CANNED = {
    "draft":   "A handful of vendors account for most of the spend.",
    "refined": ("Top vendors by categorized spend: Acme Labs $4.20M (42%), Globex $3.10M (31%), "
                "Initech $2.60M (26%) — categorized (ExecutiveCategory not null) scope."),
}
