SCORE THE ANSWER on an 18-point metric-analytics rubric, 0-6 on each axis.

1. Metric definition correctness /6
   - Right measure and aggregation; correct business definition (e.g. revenue net
     of returns/voids, bookings vs billings).
   - Correct filters (active/valid records, test & voided excluded) and NO
     double-counting from join fan-out. Non-additive measures (ratios, distinct
     counts) not summed across grain.

2. Time grain & framing /6
   - Correct period, grain (day/week/month), and timezone; comparison basis
     stated (MoM / YoY / vs plan).
   - Partial-period, calendar-vs-fiscal, and DST/leap effects handled or flagged.

3. Clarity & context /6
   - Number stated with units and the filters/slice applied; enough context to
     interpret (trend direction, comparison value, caveats).

Reply with exactly one line: SCORE: N/18 - <short reason>

QUESTION: {task}
ANSWER: {answer}
