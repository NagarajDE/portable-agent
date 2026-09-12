# Use-case packs

Each folder here is ONE agent. Start with the authoring guide:
[`../docs/concepts/use-case-pack-anatomy.md`](../docs/concepts/use-case-pack-anatomy.md), and copy
[`_TEMPLATE/`](_TEMPLATE) to begin a new pack.

| pack | mode | notes |
|---|---|---|
| `dq_qals` | data quality (SQL) | ships its own rubric override |
| `kpi_analytics` | KPI / metrics (SQL) | ships its own rubric override |
| `anomaly_rca` | root-cause (SQL) | ships its own rubric override |
| `parity_hana_snowflake` | cross-engine parity (SQL) | ships its own rubric override |
| `inventory_balance` | inventory analytics (SQL, live Cortex) | own rubric; mirrors a native agent's house rules |
| `goa_spend` | indirect spend analytics (SQL) | mirrors native GOA_SPEND_ANALYTICS_AGENT (view INDIRECT_SPEND) |
| `icertis_procurement` | active-contract analytics (SQL) | mirrors ICERTIS_PROCUREMENT_AGENT (Analyst side) |
| `procurement_contracts` | iCertis contract analytics (SQL) | mirrors PROCUREMENT_CONTRACT_AGENT (view ICERTIS_CONTRACTS) |
| `api_assistant` | generic NON-SQL example | tool layer (mock catalog + http mock); excludes `sql_safety`; input/output exemplars |
| `_TEMPLATE` | starter | copy to create a new pack |

Composition (inherit / override / concatenate) and the exact merge semantics are documented in the
authoring guide above.
