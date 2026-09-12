"""MOCK-only canned answers for the incident-triage pack. Used when WORKER_PROVIDER=mock (the mock
worker can't emit JSON tool-calls, so gathering is skipped and this canned answer is scored). With a
real model the answer is written from the tools' live observations instead."""

CANNED = {
    "draft": "orders-api appears to have an issue; checking ownership, health, and recent deploys.",
    "refined": ("orders-api (owned by team-fulfillment, tier 1) is DEGRADED with 1 open incident, "
                "immediately after the v2.4.1 deploy 12m ago — most likely a bad rollout. Next: page "
                "team-fulfillment and consider rolling back v2.4.1."),
}
