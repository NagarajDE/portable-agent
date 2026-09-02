SCORE THE ANSWER on an 18-point data-quality rubric, 0-6 on each axis.

1. Rule correctness /6
   - Correct DQ dimension for the question: completeness (nulls), uniqueness
     (dupes), validity (range/domain/format), consistency (cross-field/table),
     referential integrity (orphans), or timeliness (freshness/SLA).
   - Correct business key and grain; composite keys handled; no false positives
     from NULL-collision or untrimmed strings.

2. Evidence & quantification /6
   - Findings backed by real numbers: both an absolute count AND a rate
     (e.g. "1,240 of 50,000 = 2.5%"), plus concrete offending keys/rows.
   - Severity compared to a baseline or prior run where relevant, not asserted.

3. Actionability /6
   - States WHERE (table/column/pipeline), HOW bad (severity/threshold), and the
     likely root cause or the next concrete step to remediate.

Reply with exactly one line: SCORE: N/18 - <short reason>

QUESTION: {task}
ANSWER: {answer}
