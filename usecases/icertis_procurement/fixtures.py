"""Demo-only mock data for icertis_procurement (mirrors ICERTIS_PROCUREMENT_AGENT). Bypassed on live."""

class MockSQLTool:
    def ask(self, question: str) -> str:
        return ("VENDOR    | SOW_VALUE_USD\n"
                "Northwind | 5400000\n"
                "Umbrella  | 3200000\n"
                "Stark Ind | 2100000\n"
                "(top 3 active SOW vendors)")

CANNED = {
    "draft":   "A few vendors hold most of the active SOW value.",
    "refined": ("Top vendors by active SOW value: Northwind $5,400,000 (50.4%), Umbrella $3,200,000 "
                "(29.9%), Stark Ind $2,100,000 (19.6%)."),
}
