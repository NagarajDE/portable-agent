"""Demo-only data for the inventory-balance use case. Bypassed when SQL_TOOL != mock
(i.e. once SQL_TOOL=cortex points at the real INVENTORY_ANALYTICS semantic view)."""

class MockSQLTool:
    def ask(self, question: str) -> str:
        return ("LOCATION | ON_HAND_BALANCE\n"
                "SD-01    | 12340\n"
                "SD-02    | 8915\n"
                "SG-01    | 4220\n"
                "(total on-hand: 25475 units across 3 locations)")

CANNED = {
    "draft":   "The data shows on-hand inventory across several locations.",
    "refined": ("Total on-hand inventory balance is 25475 units across 3 locations: "
                "SD-01 (12340), SD-02 (8915), SG-01 (4220). SD-01 holds ~48% of stock — "
                "the largest single contributor."),
}
