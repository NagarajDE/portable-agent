"""Demo-only mock data for procurement_contracts (mirrors PROCUREMENT_CONTRACT_AGENT). Bypassed on live."""

class MockSQLTool:
    def ask(self, question: str) -> str:
        return ("SUPPLIER  | CONTRACT_VALUE_USD\n"
                "Wayne Ent | 8100000\n"
                "Oscorp    | 4500000\n"
                "Tyrell    | 2900000\n"
                "(top 3 active contracts by value)")

CANNED = {
    "draft":   "Contract value concentrates in a few suppliers.",
    "refined": ("Top active-contract value by supplier: Wayne Ent $8,100,000 (52.3%), Oscorp "
                "$4,500,000 (29.0%), Tyrell $2,900,000 (18.7%)."),
}
