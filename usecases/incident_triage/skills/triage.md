# Incident triage — how to reason

- Establish OWNERSHIP first (which team owns the affected service) so the summary is actionable.
- Correlate the CURRENT health signal with the MOST RECENT deploy: a status change right after a
  rollout is the leading hypothesis (likely a bad deploy) — call it out and suggest a rollback.
- Distinguish healthy from degraded/down explicitly; report open-incident counts when available.
- Keep the summary to: likely cause · owning team · next action. Do not speculate beyond the evidence.
