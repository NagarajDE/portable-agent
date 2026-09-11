"""Demo-only canned text for the generic API-assistant example pack.

This pack has NO SQL tool: it declares `tools:` (a mock service catalog + the generic http tool
in mock mode) in config.yaml, so the engine runs those read-only tools and never touches
get_sql_tool. The MOCK worker/judge still needs a domain answer to fake -- MockClient loads
CANNED from here (draft on generate, refined on each improve round). Bypassed entirely when
WORKER_PROVIDER is a real model. The `refined` text is what the loop converges to, so it must
contain the substrings the golden set checks (orders-api / team-fulfillment / healthy)."""

CANNED = {
    "draft": "The orders service looks healthy and is owned by the fulfillment team.",
    "refined": ("orders-api is healthy (uptime 99.98%, 0 open incidents) and is owned by "
                "team-fulfillment (tier 1)."),
}
