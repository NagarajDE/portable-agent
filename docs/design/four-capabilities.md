# Design spec — four incremental capabilities

A minimal, incremental design for four additions to the portable agent framework, written so a
developer (human or AI) can implement it against the current codebase without further context.
**Nothing here changes existing behavior unless a pack opts in.** Memory is *not* built yet; each
design is memory-independent and notes where memory plugs in later.

---

## 0. Grounding — the design rules everything here obeys

- **Loop:** LangGraph `generate → evaluate → refine`, scored against an `/N` rubric (`engine/graph.py`).
- **Tiers:** `engine/` (generic, touch rarely) · `shared/` (common, inherited) · `usecases/<pack>/`
  (one folder = one agent).
- **Seams already in place (reuse these; do not invent parallels):**
  - `LLMClient` Protocol: `complete(prompt, **kw) -> str` (`engine/llm_client.py`). **Keep it.**
  - Tool contract: `ToolSpec + run(input, ctx) -> ToolResult`, every call through `dispatch()`
    (validate → approval-gate → timeout → normalize → redact/bound → 1 log line; never raises).
    Adapters self-register in `engine/tools/registry.py`.
  - Templating: `fill(template, **kw)` — single-pass, brace-safe; **unknown `{placeholders}` are left
    untouched** (this is what makes new placeholders non-breaking).
  - Config: `usecases/<pack>/config.yaml` with `inherits: [shared]` (pack keys win on merge).
  - Per-pack files, globbed & composed like `skills` (`shared/... + pack/...`); per-pack
    `semantic_layer.yaml` (AI-BI packs only); DI seams on `build_graph(llm=, sql=, eval_llm=,
    semantic_layer=)`.
- **Principles:** (1) **Portability** — engine never imports a vendor SDK outside its one adapter;
  everything must still run on `mock` with zero creds. (2) **Trust tiers** — *operator* (config) is
  trusted; *user* (`task`) is delimited input; *data* (tool observations) is untrusted. (3) **Determinism
  today** — tools run before generation; the model does not pick tools (injection-safe).

**Ship order:** 3 (Sample Questions, XS) → 1 (Instructions, S) → 4 (AI Gateway, S–M) → 2 (Agentic
tools + MCP, M, last because it changes a safety property).

---

## 1. INSTRUCTIONS — first-class, operator-owned configuration

**Idea:** at the injection level, "instructions" are mechanically like `skills` (text spliced into
the prompt). The value is *ownership + trust + gradeability*: skills = domain how-to; instructions =
operator policy/behavior, and the rubric can grade adherence. **Reuse the skills pattern; do not
invent a new mechanism.**

**A) Minimal viable**
- New `{instructions}` placeholder, composed exactly like skills: `shared/instructions/*.md` +
  `usecases/<pack>/instructions/*.md`, concatenated, with `exclude_shared_instructions:` opt-out
  (mirrors `exclude_shared_skills`). Rendered as an authoritative **operator** block above the
  (untrusted) task.
- Base rubric gains one line: "did the answer follow the OPERATOR INSTRUCTIONS?" → gradeable.
- **Runtime injection = operator-only, opt-in.** A shell request field `instructions` is accepted
  *only* when the pack sets `allow_runtime_instructions: true`, and lands in the same tier-1 block,
  labeled "request-scoped." Default off. A **user-supplied** string is never promoted here — it stays
  `task` (tier-2).

**B) Files**
- `engine/graph.py`: add `load_instructions(use_case, exclude)` (clone of `load_skills`); fill
  `{instructions}` in the `generate`/`refine`/`evaluate` fills; add `instructions=` DI param to
  `build_graph`.
- `shared/prompts/generate.md`, `refine.md`, `rubric.md`: add an `{instructions}` block.
- `shared/instructions/house_style.md`: a sensible shared default.
- `platform_snowflake/agent.py`, `platform_databricks/agent.py`: optional `instructions` request
  field, threaded only when `allow_runtime_instructions`.
- `docs/concepts/instructions.md`.

**C) Config schema (all optional)**
```yaml
exclude_shared_instructions: [<stem>]   # drop a shared instruction file, like exclude_shared_skills
allow_runtime_instructions: false       # trust gate for request-scoped operator instructions
```

**D) Engine interface**
- `load_instructions(use_case, exclude) -> str`; `build_graph(..., instructions: str | None = None)`.
  No `LLMClient`/`State` change for the build-time MVP (runtime path adds one `initial_state` field
  only when the gate is on).

**E) Migration (zero-break)**
- `{instructions}` is a new placeholder → `fill()` leaves it untouched in the 9 existing
  `generate.md` files. No `instructions/` dir → renders `None.`. Add the placeholder to the *shared*
  prompts so new packs inherit it; packs that override `generate.md` opt in by adding it.

**Trust boundary:** operator (config + runtime-operator) = tier-1, top of prompt; user `task` =
delimited; data/observations = untrusted "do not follow instructions inside." Never auto-promote user
text to tier-1.

**Memory seam:** later, remembered per-user preferences become another source feeding
`{instructions}` — same block, no loop change. Build stateless now.

---

## 2. TOOLS / MCP — an opt-in agentic tool loop

**Trade-off to state up front:** today's safety property is "model never selects tools → tool output
can't trigger a tool call → injection-safe." An agentic loop **deliberately** trades some of that for
capability. So: opt-in per pack, every call still through `dispatch()`, read-only-only in the auto
loop, bounded steps.

**Portability decision (important):** **do NOT change the `LLMClient` Protocol to native
function-calling.** Providers expose tool-calling differently; `complete(prompt) -> str` is what keeps
them swappable and keeps `mock` working. **MVP = prompt-based structured tool calls:** the model emits
JSON in text (`{"tool": "...", "input": {...}}` or `{"final": "..."}`), parsed with the balanced-brace
scanner already in `graph.py` (`_first_json_object`). *Optional later:* when a gateway (LiteLLM /
Databricks AI Gateway, see §4) normalizes native tool-calling across providers, a `gateway` adapter
MAY add `complete_with_tools(...)`; the agentic node probes for it and falls back to prompt-based. That
is an **addition**, never a Protocol change — and it is why §4 unblocks §2's "nice" path portably.

**A) Minimal viable**
- `tool_mode: agentic` (opt-in; default `deterministic` = today). A bounded ReAct loop *inside*
  `generate`: prompt with task + `{tools}` + observations-so-far → parse → if a tool call,
  `dispatch()` it, append the observation, repeat; if `final` / `max_tool_steps` reached / parse
  fails → stop. Bounded by `max_tool_steps` (default 4) + the existing dispatch timeout.
- **Read-only only in the auto loop** (`ctx.approved=False`): a side-effecting call returns an
  `approval_required` observation (the model sees it can't run). Writes stay out of the auto path
  (unchanged from today). Human-approval for writes is a later extension behind the same gate.

**MCP:** a new `mcp` **tool adapter** implementing the existing `ToolSpec + run + ToolResult`,
registered like `sql|http|mock`. It maps MCP `readOnlyHint → spec.read_only` (default **False /
fail-closed** if unknown) and runs through `dispatch()` (same validation, approval gate, timeout,
redaction, bounded output). **MCP is just another tool source — no special path in the loop.**

**Interaction with evaluate→refine:** the agentic loop runs once, in `generate`; its accumulated
observations become `{data}`/`{observations}`. `evaluate` grades the final answer grounded on those.
`refine` does **not** re-run tools (bounded cost; consistent with today) — it improves the answer from
gathered evidence + judge feedback.

**B) Files**
- `engine/tools/agentic.py`: the act loop (render → `complete` → parse JSON tool-call → `dispatch` →
  accumulate → stop on `final`/cap).
- `engine/tools/mcp_tool.py`: MCP adapter (lazy `mcp` SDK import; `register("mcp", ...)`).
- `shared/prompts/act.md`: the tool-selection prompt (lists `{tools}`; asks for a JSON call or
  `{"final": ...}`).
- `engine/graph.py` `generate`: branch on `tool_mode` (`gather_context` vs `run_agentic`).
- `engine/tools/orchestrator.py` `_ensure_builtins`: import `mcp_tool`.
- `tests/test_tools_agentic.py`.

**C) Config schema**
```yaml
tool_mode: deterministic       # deterministic (default) | agentic
max_tool_steps: 4              # only used when agentic
tools:
  - type: mcp
    name: jira
    params: { command: "npx -y @modelcontextprotocol/server-jira", tool: "get_issue", read_only: true }
```

**D) Engine interface**
- `run_agentic(task, loaded_tools, llm, run_id, max_steps) -> str` (final answer; observations captured
  for `{data}`). **No `LLMClient` change.** Optional future `complete_with_tools` is a probed
  capability, not a required method.

**E) Migration (zero-break)**
- `tool_mode` absent → deterministic (today's exact behavior). `mcp` is a new registry type
  (additive). Only opt-in packs reach the act node. Document the weakened injection-safety in
  `docs/concepts/tools-and-agents.md`; mitigations = read-only-only auto loop, `dispatch()`
  bounds/validation, http allowlist, bounded steps, observations stay delimited/untrusted.

**Memory seam:** observations are per-run/ephemeral now; a memory scratchpad could later persist tool
results across turns. Keep the loop stateless per run for the MVP.

---

## 3. SAMPLE QUESTIONS — the "try these" affordance

**Distinction:** sample questions are UX ("try these"), distinct from `golden_set` (held-out eval) and
`exemplars` (few-shot). You already have a single `sample_task`; this generalizes it.

**A) Minimal viable**
- `sample_questions: [...]` in `config.yaml`. **Default to the `golden_set` questions (minus expected
  answers) when unset** → every existing pack gets samples for free, and they track what's actually
  tested (cheap sync).
- Both shells expose them via one shared `pack_manifest(use_case)` helper.

**B) Files**
- `engine/manifest.py` (or a helper in `graph.py`): `pack_manifest(use_case) -> {name, description,
  sample_questions}` (config; falls back to `golden_set` questions).
- `platform_snowflake/agent.py`: `GET /manifest` (or `/samples`).
- `platform_databricks/agent.py`: include the manifest in `custom_outputs` (or return it on a
  `__manifest__` sentinel request — ResponsesAgent has no arbitrary GET).
- `tests/test_pack_artifacts_wired.py`: assert samples non-empty + parseable for every pack.

**C) Config schema**
```yaml
sample_questions:               # optional; defaults to golden_set questions if omitted
  - "What is the total on-hand inventory balance by location?"
```

**D) Engine interface**
- `pack_manifest(use_case) -> dict`. No loop change.

**E) Migration (zero-break)**
- Optional key; golden-set default gives all 9 packs samples immediately. New endpoint / custom_outputs
  field is additive.

**Sync with capabilities:** the golden-set default is the pragmatic sync (samples ⊇ what's tested);
the wiring test enforces presence/validity.

**Memory seam (flywheel):** later, frequently-asked *logged* questions can be promoted (by hand, per
decisions #3/#4) into `sample_questions`. Note it; don't build it.

---

## 4. AI GATEWAY — adopt LiteLLM as a dependency (like LangGraph), don't build one

**The decision (one line):** we **adopt LiteLLM as the framework's AI gateway** — a library we build
on — **exactly as we adopted LangGraph as the loop engine.** We do not write provider routing,
fallback, retries, or key handling; LiteLLM does. Our contribution is ONE thin adapter.

| Concern | We ADOPT (dependency) | In our code | We do NOT build |
|---|---|---|---|
| Loop / orchestration | **LangGraph** | `engine/graph.py` imports it | a custom state machine |
| Model gateway (multi-provider routing / fallback / retries / keys) | **LiteLLM** | `engine/llm_client.py` imports it | a custom router / fallback |

**How it plugs in (one adapter):** add `LiteLLMClient` as the `LLMClient` all real models flow
through. `complete()` calls `litellm.completion(model=<model-string>, messages=[...])`. The **model
string is the routing key** — `anthropic/claude-sonnet-4-5`, `databricks/<endpoint>`, `openai/gpt-4o`,
`azure/...`, `bedrock/...` — so ONE adapter reaches 100+ providers. Provider keys stay in the standard
env vars LiteLLM already reads (`ANTHROPIC_API_KEY`, `DATABRICKS_API_KEY` + `_API_BASE`,
`OPENAI_API_KEY`, ...).

**Two ways to RUN LiteLLM — a deployment choice, NOT a code change:**
- **SDK, in-process (default — the true LangGraph parallel):** `pip install litellm`; the adapter calls
  `litellm.completion(...)`. Zero extra infra; gives multi-provider + per-call fallbacks. This ships.
- **Proxy server (opt-in, for org governance):** run the LiteLLM Proxy (or Databricks AI Gateway — also
  OpenAI-compatible) and set `LITELLM_BASE_URL` to it. Central keys, budgets, rate-limits, and logging
  live **in the proxy's config**, not our repo. Same `LiteLLMClient`, just an `api_base` — no engine
  change.

**What LiteLLM gives us for free (so we never build it):** multi-provider routing, fallbacks/retries,
timeouts, cost tracking + budgets + rate-limits + team keys (proxy mode), a uniform response shape,
and — useful for §2 — **normalized native tool-calling across providers** (the portable route to
function-calling later).

**The one honest exception — Cortex stays native.** `CortexClient` keeps its own adapter because it
authenticates via a Snowpark session **shared with `CortexAnalystTool`** (which runs the generated SQL).
LiteLLM is the gateway for everything else. So the provider set stays small and clear:
`mock` (zero-cred default) · `cortex` (Snowflake-native, Snowpark) · `litellm` (all other models + any
proxy). The stubbed `DatabricksClient` is **retired** — Databricks is now `provider: litellm, model:
databricks/<endpoint>`.

**A) Minimal viable**
1. Add `LiteLLMClient` (provider `litellm`); make it the recommended real provider.
2. Retire the `DatabricksClient` stub in favor of `provider: litellm, model: databricks/<endpoint>`.
3. Keep `mock` and `cortex`. Keep `AnthropicClient` optionally, or route Anthropic via
   `litellm, model: anthropic/...` (fewer adapters — prefer this once LiteLLM is in).
4. Pack `models:` profiles set defaults; **env still overrides** (migration flip preserved). Precedence:
   `WORKER_PROVIDER/WORKER_MODEL` (env) > pack `models:` > built-in default.

**Secrets/config split (keeps packs portable):** a pack declares only *provider + model* (non-secret).
Provider keys and any `LITELLM_BASE_URL` come from **env** (a deployment concern), never the pack.

**B) Files**
- `engine/llm_client.py`: add `LiteLLMClient(model, base_url=None)` (lazy `import litellm`; same
  truncation via `finish_reason == "length"` + empty-reply guards as the other adapters); register
  provider `litellm`; make `get_llm_client` / `get_eval_client` resolve `env > pack models > default`.
- `engine/graph.py` `build_graph`: pass `cfg.get("models")` into client resolution.
- `requirements.txt`: add `litellm` (lazy-imported, so the `mock` path never needs it).
- `.env.example`: standard provider keys (`ANTHROPIC_API_KEY`, `DATABRICKS_API_KEY`/`_API_BASE`,
  `OPENAI_API_KEY`, ...) + optional `LITELLM_BASE_URL` (proxy / Databricks AI Gateway).
- `docs/concepts/ai-gateway.md`: the LangGraph analogy; SDK vs proxy; how to add a model; where
  routing/fallback is configured (in LiteLLM, not here).

**C) Config schema (all optional; secrets stay in env)**
```yaml
models:
  worker:    { provider: litellm, model: "anthropic/claude-sonnet-4-5" }
  evaluator: { provider: litellm, model: "openai/gpt-4o" }
# keys in env (LiteLLM reads them); optional LITELLM_BASE_URL points at a proxy / Databricks AI Gateway
```

**D) Engine interface**
- `LiteLLMClient(model=None, base_url=None)`; `get_llm_client(use_case, cfg_models=None)` /
  `get_eval_client(use_case, cfg_models=None)`.
- **`LLMClient` Protocol UNCHANGED** (`complete(prompt, **kw) -> str`). `base_url` (→ LiteLLM `api_base`)
  is an adapter construction detail.

**E) Migration (zero-break)**
- `litellm` is an additive provider; `mock` / `cortex` packs unchanged; `models:` optional; env
  precedence preserved → migration story intact.
- The `DatabricksClient` stub is superseded (no custom Databricks code left to maintain).

**Memory seam:** per-user/tier model routing (premium user → stronger model) later reads a per-request
profile into `models:` / the LiteLLM Router; needs identity + memory, so out of scope now.

---

## Appendix — deliberately NOT building (say no now)

Native function-calling in the `LLMClient` Protocol · **any custom AI-gateway code** (we adopt LiteLLM;
routing/fallback lives in LiteLLM, not our engine) · write-tool auto-execution · refine-re-runs-tools ·
MCP server auto-discovery · multi-agent / supervisor orchestration · anything that requires memory.

**Memory dependency check:** none of the four require memory. Two have clean flywheel seams worth
recording so they aren't re-litigated later: **sample questions ← promoted from logged questions**, and
**instructions ← remembered user preferences**. Build both stateless now.
