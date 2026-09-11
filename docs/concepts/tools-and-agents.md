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
| `ToolSpec` | tool **identity + capability metadata**: `name`, `description`, `read_only` (**required** — fail-closed authoring), optional pydantic `input_model` for argument validation |
| `ToolResult` | normalized result: `ok`, `output` (text evidence), `error`, `meta` |
| `ToolError` | normalized error: `kind` (`validation`/`approval_required`/`timeout`/`runtime`/…) + `message` |
| `ToolContext` | per-call context: `run_id` (correlation), `timeout_s`, `approved` (gate for writes) |
| `Tool` (Protocol) | anything with a `spec` and `run(input, ctx) -> ToolResult` |

Two helpers live here too: `bound_output()` (size-cap tool output) and `redact()` (scrub
secrets from log/error strings).

---

## 4. The trust boundary (`engine/tools/dispatch.py`)

Every tool call goes through `dispatch(tool, input, ctx)`, which is **total — it always returns a
`ToolResult`, never raises** (even `spec` access is defensive). In order, it:

1. **Approval gate** — a side-effecting tool (`spec.read_only is False`) runs only when
   `ctx.approved is True` (strict identity — a truthy string like `"false"` does *not* approve);
   otherwise it is *blocked and never executed*.
2. **Input validation** — arguments are validated against the tool's `input_model` *before* the
   tool runs; unknown args are **rejected** (`extra="forbid"`), and the error is **value-free**
   (it names the offending field, never echoes its — possibly secret — value).
3. **Timeout + bounded concurrency** — the call runs on a bounded worker pool
   (`TOOL_MAX_CONCURRENCY`, default 4) with a per-call `timeout_s`; a timed-out call is abandoned
   and `future.cancel()`-ed (Python can't force-kill a running thread — network tools also set
   their own transport deadline).
4. **Normalized errors + result invariants** — any exception becomes
   `ToolResult(ok=False, error=ToolError(kind, …))`, and the message is **redacted + size-bounded**
   (SDK errors can embed authenticated URLs/tokens). Invariants are enforced: `ok=False` must carry
   an error (one is synthesized if missing); `ok=True` *with* an error fails closed.
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

`load_tools` distinguishes three cases so intent is never ambiguous:

| `tools:` in config | `load_tools` returns | behavior |
|---|---|---|
| **absent** (no `tools:` key) | `None` | legacy `get_sql_tool().ask()` path — **existing SQL packs untouched** |
| **`tools: []`** (explicit empty) | `[]` | a deliberately **toolless** agent — no tools *and* no SQL |
| **`tools: [ … ]`** | list of tools | run the declared read-only tools |

A present-but-invalid `tools:` — **null**, or any non-list (`{}`, `false`, a string) — is a
**config error** and raises: only an *absent* key is the implicit legacy path, and a stray
`tools:` (null) is treated as a mistake rather than silently meaning legacy. Use `tools: []` for a
deliberately toolless agent.

---

## 6. The three built-in tools

Registered in the `engine/tools/registry` on import; construct via `build_tool(type, params)`.

| type | file | what it does | safety notes |
|---|---|---|---|
| `sql` | `sql_bridge.py` | **compatibility layer** — wraps `engine.sql_tool.get_sql_tool()` so SQL is one tool category. New multi-tool packs use it; legacy packs don't (they keep the direct path). | read-only; vendor SDK stays lazy; forwards `ctx.timeout_s` to adapters whose `ask()` accepts it |
| `mock` | `mock_tool.py` | deterministic in-process tool (canned output / echo). The generic stand-in for creds-free demos and tests. | `read_only` configurable *only* on the mock, so tests can exercise the approval gate |
| `http` | `http_tool.py` | the **one generic HTTP GET reference**. Any API-backed pack reuses it with config — no vendor connectors. | see below |

The `http` tool is the reference implementation of the network-safety contract:

- **Allowlist, fail-closed** — calls only configured hosts; an unset *or explicitly empty*
  (`allow_hosts: []`) allowlist denies everything. A bare string is treated as one host.
- **SSRF guard** — the target must not resolve to a private/loopback/link-local/reserved/CGNAT
  (`100.64/10`)/unspecified IP; IPv4-mapped IPv6 is unwrapped; an empty DNS answer fails closed.
- **Per-hop, per-connect revalidation** — redirects are followed *manually*, and the host +
  SSRF check runs immediately *before every connect* (each redirect hop and retry), shrinking the
  DNS-rebinding window. (Residual: the resolved IP is not pinned onto the socket — the **allowlist
  is the primary control**, since rebinding still requires attacker DNS for an allowlisted host.)
- **Origin-bound secret** — the bearer token (from the env var *named* by `token_env`) is sent
  **only to the original request origin and only over https** — never forwarded to a redirected
  origin, never over an http downgrade, never written in a pack, never logged.
- **No ambient auth** — a private `trust_env=False` session (no env proxies / `.netrc`); URL
  userinfo is rejected; `path` must be **relative** (no absolute URL, no `..` escape).
- **Bounded** — response size cap, per-call timeout *and* a wall-clock deadline across all
  redirects/retries, bounded retries with backoff, optional rate limit; the response is always closed.
- **Untrusted output** — the body is returned as plain text evidence, never executed.
- **Capability** — a GET is `read_only=True` by default; a pack can set `read_only: false` for a
  known-mutating endpoint so the approval gate applies.

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
