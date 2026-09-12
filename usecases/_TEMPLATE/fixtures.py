"""Demo-only data for the _TEMPLATE pack (mock provider). Bypassed once SQL_TOOL / WORKER_PROVIDER
are real. Replace the rows and the canned answers with your domain's."""

class MockSQLTool:
    def ask(self, question: str) -> str:
        return ("ITEM  | VALUE\n"
                "alpha | 10\n"
                "beta  | 7\n"
                "(total value: 17 across 2 items)")

# MockClient (engine/llm_client.py) reads these on the mock provider: "draft" for generate,
# "refined" for each refine. Keep both keys.
CANNED = {
    "draft":   "The items carry some value.",
    "refined": "Total value across items is 17 (alpha 10, beta 7); alpha is the largest at ~59%.",
}
