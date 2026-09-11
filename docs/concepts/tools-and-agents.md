# Tools and agents — how an agent uses tools (and why SQL is just one of them)

This explains the **generic tool layer** in [`engine/tools/`](../../engine/tools/): the typed
interface that lets one agent use one or more named tools, of which **text-to-SQL is now just
one category**. It preserves every existing contract — a pack that declares no tools takes the
identical legacy single-SQL path — while making *which* tools an agent uses a matter of
**config, not code**.

> Prereqs: [`the-refine-loop.md`](the-refine-loop.md) (the generate→evaluate→refine loop this
> plugs into) and [`access-control.md`](access-control.md) (who may invoke an agent, and the
> service-role identity its reads run as).

---

## 1. The idea in one picture

The loop never changed. Only the **source of the context** that `generate` reasons over did:

```
                 ┌──────────────────────── engine/graph.py (unchanged loop) ────────────────────────┐
question ─►      │  GENERATE ─► EVALUATE ─► (refine?) ─► … ─► best answer                            │
                 └───────▲──────────────────────────────────────────────────────────────────────────┘
                         │ context
          ┌──────────────┴───────────────┐
   legacy pack (no `tools:`)      tools pack (`tools:` declared)
   data = sql.ask(task)           data = gather_context(task, tools)   ← runs each READ-ONLY tool,
   (byte-for-byte as before)                                              concatenates their observations
```

Both feed a single `data` string into `generate`. A legacy pack's `data` is exactly today's
`sql.ask(task)`; a tools pack's `data` is the labeled observations from its declared tools.

---

## 2. Deterministic tool execution (not ReAct — yet)

**The model does not choose tools.** The pack lists them; the engine runs the read-only ones up
front (`gather_context`) and hands their output to `generate`. This is *retrieve-then-reason*, the
same shape as the SQL path — just generalized to N tools.

Why this matters beyond simplicity: because the LLM never *selects* a tool or its arguments,
**tool output cannot trigger a tool call**. A prompt-injection string sitting inside a returned
API body is only ever *data the answer is scored against* (the rubric marks observations
untrusted and delimits them) — it can't escalate into an action. That's a real security property
of the deterministic design, not an accident.

LLM-driven tool selection (**ReAct**: the model picks which tool to call, with what args, in a
loop) is **deliberately deferred**. When it's built it slots in *behind the same `dispatch()`* —
the validation, approval gate, timeouts, and normalized errors below apply unchanged — without
touching the loop or any pack.

---

## 3. The contracts (`engine/tools/base.py`)

Small, typed, vendor-free — the interface everything else references:

| type | what it is |
|---|---|
| `ToolSpec` | tool **identity + capability metadata**: `name`, `description`, `read_only`, optional pydantic `input_model` for argument validation |
| `ToolResult` | normalized result: `ok`, `output` (text evidence), `error`, `meta` |
| `ToolError` | normalized error: `kind` (`validation`/`approval_required`/`timeout`/`runtime`/…) + `message` |
| `ToolContext` | per-call context: `run_id` (correlation), `timeout_s`, `approved` (gate for writes) |
| `Tool` (Protocol) | anything with a `spec` and `run(input, ctx) -> ToolResult` |

Two helpers live here too: `bound_output()` (size-cap tool output) and `redact()` (scrub
secrets from log/error strings).

---

## 4. The trust boundary (`engine/tools/dispatch.py`)

Every tool call goes through `dispatch(tool, input, ctx)`, which is **total — it always returns a
`ToolResult`, never raises**. In order, it:

1. **Approval gate** — a side-effecting tool (`spec.read_only is False`) runs only when
   `ctx.approved` is True; otherwise it is *blocked and never executed*.
2. **Input validation** — arguments are validated against the tool's `input_model` *before* the
   tool runs; malformed args become a `validation` error (the tool never sees them).
3. **Timeout + bounded concurrency** — the call runs on a bounded worker pool
   (`TOOL_MAX_CONCURRENCY`, default 4) with a per-call `timeout_s`.
4. **Normalized errors** — any exception becomes `ToolResult(ok=False, error=ToolError(kind, …))`;
   a buggy tool can never crash the loop.
5. **Bounded output** — result output is size-capped (`bound_output`).
6. **Observability** — exactly **one** structured, **redacted** JSON log line per call
   (`tool`, `status`, `ms`, `run_id`) on the `portable_agent.tools` logger — never a secret or a
   raw payload.

---

## 5. The orchestrator (`engine/tools/orchestrator.py`) and the `tools:` config

`load_tools(use_case, cfg)` builds the pack's declared tools; `gather_context(task, tools, run_id)`
runs the **read-only** ones (with `${task}` substituted into each tool's `input`) and returns their
labeled, bounded observations. Side-effecting tools are **skipped** by the auto-run path (they need
explicit approval, which the deterministic observe step never grants).

A pack opts in with a `tools:` list in its `config.yaml` (fully additive — omit it for the legacy
path):

```yaml
tools:
  - type: mock              # registry key: mock | sql | http
    name: catalog           # label shown in observations/logs (optional; defaults to type)
    input: { query: "${task}" }   # per-tool input; ${task} is substituted at run time
    params: { output: "…" } # adapter construction params (optional)
```

`load_tools` returns `[]` when there is **no `tools:` key** — that is the signal
`engine/graph.py` uses to take the identical `get_sql_tool().ask()` path. **Existing SQL packs are
untouched.**

---

## 6. The three built-in tools

Registered in the `engine/tools/registry` on import; construct via `build_tool(type, params)`.

| type | file | what it does | safety notes |
|---|---|---|---|
| `sql` | `sql_bridge.py` | **compatibility layer** — wraps `engine.sql_tool.get_sql_tool()` so SQL is one tool category. New multi-tool packs use it; legacy packs don't (they keep the direct path). | read-only; vendor SDK stays lazy |
| `mock` | `mock_tool.py` | deterministic in-process tool (canned output / echo). The generic stand-in for creds-free demos and tests. | `read_only` configurable *only* on the mock, so tests can exercise the approval gate |
| `http` | `http_tool.py` | the **one generic HTTP GET reference**. Any API-backed pack reuses it with config — no vendor connectors. | see below |

The `http` tool is the reference implementation of the network-safety contract:

- **Allowlist, fail-closed** — calls only configured hosts; an empty allowlist denies everything.
- **SSRF guard** — the target must not resolve to a private/loopback/link-local/reserved IP
  (blocks `169.254.169.254`, `127.0.0.1`, `10/8`, …).
- **Per-hop redirect validation** — redirects are followed *manually* and every hop is
  re-checked against the allowlist + SSRF guard.
- **Secrets from env only** — a bearer token is read from an env var *named* by the pack's
  `token_env`; the value is never written in a pack and never logged.
- **Bounded** — response size cap, per-call timeout, bounded retries with backoff, optional
  rate-limit interval.
- **Untrusted output** — the body is returned as plain text evidence, never executed.

Default `mode` is `mock` (canned, no network), so packs and tests run with no credentials; set
`mode: http` + an allowlist to make real calls.

---

## 7. The safety model, at a glance

| concern | how it's handled |
|---|---|
| **Secrets** | env only (`token_env` names the var); redacted from logs/errors; never in packs/prompts |
| **Least privilege** | only configured tools exist; read-only is the default; the auto-run path never runs writes |
| **Side effects** | explicit `read_only=False` in the tool's spec + `ctx.approved` required to run |
| **Input** | validated against a pydantic `input_model` before the tool runs |
| **Network** | allowlist + SSRF guard + per-hop redirect re-validation (http tool) |
| **Output** | bounded size, treated as untrusted evidence |
| **Prompt injection** | deterministic execution → tool output can't trigger a tool call; rubric marks observations untrusted |
| **Reliability** | timeouts, bounded concurrency, bounded retries with backoff, rate-limit option |
| **Observability** | one redacted structured log line per call (tool · status · ms · run_id) |

---

## 8. Adding a tool, and adding a tool-using pack

**A new tool type** = a class with a `spec` and `run(input, ctx)`, plus one `register("name",
Factory)` call at import (mirror `mock_tool.py`). Keep any vendor SDK import lazy (inside `run`),
per the core principle in [`CLAUDE.md`](../../CLAUDE.md). No engine or loop change is needed.

**A new tool-using pack** = a normal pack (see the README "Add a new agent" section) whose
`config.yaml` adds a `tools:` list, and whose `prompts/generate.md` uses the `{observations}` and
`{tools}` placeholders instead of `{data}`. See the worked, creds-free example in
[`usecases/api_assistant/`](../../usecases/api_assistant/) (a mock catalog + the http tool in mock
mode; runs on the mock provider with `python run_evals.py api_assistant`).

---

## See also
- [`engine/tools/`](../../engine/tools/) — `base` · `registry` · `dispatch` · `orchestrator` · the three adapters.
- [`usecases/api_assistant/`](../../usecases/api_assistant/) — the generic, non-SQL example pack.
- [`the-refine-loop.md`](the-refine-loop.md) — the loop the observations feed into.
- [`access-control.md`](access-control.md) — invoke access vs. the identity reads run as.
- [`CLAUDE.md`](../../CLAUDE.md) — the tool-layer invariants (don't let config upgrade `read_only`; keep vendor SDKs lazy).
