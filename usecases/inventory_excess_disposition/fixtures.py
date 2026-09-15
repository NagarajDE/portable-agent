"""Demo-only mock data for the excess-disposition pack. Bypassed once SQL_TOOL=cortex points at the
real INVENTORY_ANALYTICS view. Routes on the QUESTION so the multi-step chain returns DISTINCT evidence
per step (excess ranking vs. forward demand) -- this is what makes the offline planned run meaningful.

Worked shape the mock encodes:
  M-1001: excess ~= forward demand      -> Hold (self-correcting)
  M-2044: high excess, NO forward demand -> Disposition review
  M-3910: low local demand, cross-plant demand present -> Redistribute
"""

_EXCESS = ("MATERIAL | PLANT | STORAGE_LOCATION | MRP_CONTROLLER | EXCESS_QTY | MAX_INVENTORY | TOTAL_EXCESS_STOCK_VALUE\n"
           "M-1001   | 3300  | RM01 | C12 | 5000 | 2000 | 480000\n"
           "M-2044   | 3300  | FG02 | C07 | 1200 |  800 | 260000\n"
           "M-3910   | 3100  | RM01 | C12 |  900 |  600 | 145000\n"
           "(grain: Material x Plant x Storage Location; most recent snapshot; top 3 by excess value)")

_DEMAND = ("MATERIAL | FORECAST_DEMAND_QTY_QTR_END_SUM | CROSS_PLANT_DEMAND_QTY\n"
           "M-1001   | 4800 | 0\n"
           "M-2044   | 0    | 0\n"
           "M-3910   | 200  | 1500\n"
           "(quarter-end forecast; cross-plant demand from other plants)")


class MockSQLTool:
    def ask(self, question: str) -> str:
        q = (question or "").lower()
        if "demand" in q or "forecast" in q:            # the s2 (forward-demand) step
            return _DEMAND
        if "excess" in q or "value" in q:               # the s1 (excess-ranking) step
            return _EXCESS
        return _EXCESS                                   # default to the excess snapshot


CANNED = {
    "draft": "Top excess positions identified; classifying by forward demand.",
    "refined": ("Disposition for the top excess by value: M-1001 ($480k, 5000 excess) — Hold, forward "
                "demand 4800 will consume it (self-correcting). M-2044 ($260k, 1200 excess) — Disposition "
                "review, no forward demand. M-3910 ($145k) — Redistribute, cross-plant demand 1500."),
}
