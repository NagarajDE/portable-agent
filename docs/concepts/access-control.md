# Access control — who can call an agent, and what it can read

Access control here is **two separate questions**. Keeping them apart is the whole key to
reasoning about it:

1. **Who may invoke the agent?** — *invoke access* (endpoint-level)
2. **What data/tools may the agent reach once called?** — *execution identity* (data-level)

Native vendor agents (Cortex Agents, Databricks Genie) answer both with the platform's RBAC. This
design answers #1 the same way, and answers #2 at the **deployment** level today — with true
per-user enforcement as a recorded **end goal** (see the last section).

---

## 1. Who may invoke the agent (endpoint access) — solved today

**One deployment = one use case.** The serving shell fixes the pack via the `USE_CASE` env var, so
each use case is its **own service with its own endpoint**. You then grant who can call each
endpoint using the platform's native RBAC — exactly like granting access to a native agent:

| Platform | Endpoint access control |
|---|---|
| **Snowflake (SPCS)** | Grant service/endpoint access per role. Role A can reach the `dq_qals` service, role B the `inventory_balance` service, etc. |
| **Databricks** | Model Serving endpoint ACLs (`CAN_QUERY`) per user/group, per endpoint. |

So *"not everyone can use every agent"* = **deploy per use case + grant the endpoint per role.**
This maps 1:1 to the native "control access at each agent" model.

```
role: sales_team   ──►  [inventory_balance service]   (granted)
role: sales_team   ──╳   [finance_kpi service]        (not granted → can't invoke)
```

---

## 2. What data the agent can read (execution identity) — the real control

Once invoked, the loop runs SQL against the warehouse **as some identity**. That identity decides
what data is reachable — and it is the *primary* access control in the whole design (the SQL
read-only scanner is only defense-in-depth on top of it).

**Today that identity is the SERVICE's role, not the end user's.** Consequences:

- ✅ **Per-use-case data scoping works cleanly.** Give each use-case service a **narrowly-scoped
  role** — `SELECT` only on that domain's tables + that domain's semantic view. The DQ agent's role
  *physically cannot* read inventory data, no matter what SQL the model generates. This is the
  control to rely on (`CLAUDE.md`: "the PRIMARY control is granting the service role SELECT-only").
- ⚠️ **Per-end-user control does NOT happen automatically.** Because every query runs as the one
  service role, two different users hitting the same endpoint see the **same rows**. Row-level
  security (RLS), column masking, and per-user grants are **not** applied per caller.

```
User A ─┐                          ┌─ runs SQL as ─► SERVICE_ROLE ─► sees SERVICE_ROLE's data
        ├─►  [use-case endpoint] ──┤
User B ─┘                          └─ (same role for everyone → same data for A and B)
```

This is the one spot where native agents currently have an edge: they can run in the **caller's**
context, so the user's own grants/RLS apply for free. This design trades that for portability — the
loop is deliberately identity-agnostic.

---

## 3. How to enforce access — coarse to fine

Pick the granularity your governance actually needs:

| Approach | Granularity | Status | Effort |
|---|---|---|---|
| **Per-use-case service + scoped role** | per use case / per domain | ✅ works today | low — grants + one deploy per pack |
| **Per-audience services** (e.g. `sales_inventory`, `finance_inventory`, each a service whose role matches that audience's data) | per audience/group | ✅ works today | low-medium — more deployments |
| **Caller-identity propagation** — pass the end-user's token so SQL runs *as the user* → their RLS/masking/grants apply | **true per-user** | 🅿️ parked (end goal) | high, platform-specific |

**Rule of thumb: make the access boundary the deployment boundary.** Deploy one agent per
(use case × audience), each with a role that can see only what that audience should. That covers the
large majority of real "not everything should be accessible" requirements without any custom authz.

---

## 4. The end goal (deferred): pass the user's identity for true RLS

The target state is to **propagate the caller's identity into SQL execution**, so queries run **as
the user** and Snowflake/Databricks apply that user's **row-level security, column masking, and
grants automatically** — matching native caller's-rights agents. When that lands, per-user access
needs *zero* app-level authz: the warehouse enforces it, and the agent inherits it.

What it takes (sketch, for when we build it):

- **Snowflake:** the SPCS ingress already carries the authenticated caller's identity to the
  container; the missing piece is a **token exchange** so `snowpark_session()` opens the session in
  the caller's context instead of the service role. (Today `snowpark_session()` uses the service's
  injected OAuth token / a single PAT locally.)
- **Databricks:** run the model/SQL under the invoking user's credentials (on-behalf-of), rather
  than the endpoint's service principal.

Until then: **service-role identity, scoped per deployment.** This is recorded as the access-control
end goal in `PROJECT_CONTEXT.md` (§12.5 and the Open items table), to be addressed later.

> Do not confuse this with the SQL read-only heuristic (`_ensure_read_only` in
> `engine/sql_tool.py`). That stops *writes*; it is not an access-control boundary. Access is the
> role's grants (today) and, eventually, the caller's identity.

---

## See also
- `PROJECT_CONTEXT.md` §12.5 (access control across agents) + decision #11 (agent-level grants).
- `CLAUDE.md` — the SELECT-only service role as the primary control; the read-only heuristic as defense-in-depth.
- `docs/deployment/` — where per-service roles and grants are set at deploy time.
