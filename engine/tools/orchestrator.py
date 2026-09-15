"""
ENGINE / tools / orchestrator -- the DETERMINISTIC bridge between a pack's declared tools and
the loop. No LLM decides anything here: the pack config lists the tools, the engine runs the
READ-ONLY ones with inputs rendered from the task, and their outputs become the context the
`generate` node reasons over. (This is why tool output can never trigger a tool call -- a
strong prompt-injection property. LLM-driven tool selection / ReAct would slot in here later,
behind the same dispatch(), without changing the loop or the packs.)

Config shape (usecases/<pack>/config.yaml), fully additive -- packs with NO `tools:` key keep
the legacy single-SQL path in engine/graph.py unchanged:

    tools:
      - type: mock              # registry key (mock | sql | http | ...)
        name: catalog           # label for logs/observations (optional; defaults to type)
        input: { query: "${task}" }   # per-tool input; ${task} is substituted at run time
        params: { responses: {...} }  # adapter construction params (optional)
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from engine.sql_tool import no_data          # shared blank-retrieval sentinel (stdlib-only module: no cycle)
from engine.tools.base import ToolContext, Tool
from engine.tools.dispatch import dispatch
from engine.tools.registry import build_tool


@dataclass
class LoadedTool:
    label: str
    tool: Tool
    input_template: dict


def _ensure_builtins() -> None:
    """Import the builtin adapters so they self-register. Import-only (their heavy/vendor
    deps stay lazy inside run()), and idempotent."""
    from engine.tools import sql_bridge, mock_tool, http_tool, mcp_tool  # noqa: F401


def load_tools(use_case: str | None, cfg: dict) -> list[LoadedTool] | None:
    """Build the pack's declared tools. Routing is explicit (M6/M17):
      * `tools:` key ABSENT               -> return None -> caller takes the legacy SQL path
        (the ONLY implicit path -- existing packs are untouched).
      * `tools: []`                        -> return []   -> a DELIBERATELY toolless agent
        (no tools AND no SQL).
      * `tools: [ ... ]`                   -> return the built tools.
      * `tools:` present but null / not a list -> raise (never silently fall through). `tools: null`
        is almost always an accident, so it errors with guidance rather than quietly meaning legacy."""
    _ensure_builtins()
    if "tools" not in cfg:
        return None                                   # legacy: no tools declared -> SQL path
    entries = cfg["tools"]
    if entries is None:
        raise ValueError("`tools:` is null; use `tools: []` for a toolless agent, or OMIT the key "
                         "entirely for the legacy SQL path")
    if not isinstance(entries, list):
        raise ValueError(f"`tools:` must be a list of tool entries, got {type(entries).__name__}")
    loaded: list[LoadedTool] = []
    for entry in entries:
        if not isinstance(entry, dict) or "type" not in entry:
            raise ValueError(f"each tool entry needs a `type`; got {entry!r}")
        ttype = str(entry["type"]).strip().lower()    # normalize (M5): 'SQL'/' sql ' -> 'sql'
        label = entry.get("name", ttype)
        params = dict(entry.get("params") or {})
        params.setdefault("tool_name", label)
        if ttype == "sql":                            # inject what the SQL bridge needs
            params.setdefault("use_case", use_case)
            params.setdefault("default_sql_tool", cfg.get("default_sql_tool", "mock"))
        tool = build_tool(ttype, params)              # raises UnknownToolError on a bad type
        loaded.append(LoadedTool(label=label, tool=tool,
                                 input_template=dict(entry.get("input") or {})))
    return loaded


def _render(template: dict, task: str) -> dict:
    """Substitute ${task} into string values of the input template (recursing into nested
    dicts/lists). Deterministic -- no model involved."""
    def sub(v):
        if isinstance(v, str):
            return v.replace("${task}", task)
        if isinstance(v, dict):
            return {k: sub(x) for k, x in v.items()}
        if isinstance(v, list):
            return [sub(x) for x in v]
        return v
    return {k: sub(v) for k, v in (template or {}).items()}


def _tool_context(run_id: str) -> ToolContext:
    timeout = max(1.0, float(os.getenv("TOOL_TIMEOUT_SECONDS", "30")))
    # approved defaults False: the auto-observe step NEVER runs side-effecting tools.
    return ToolContext(run_id=run_id or "-", timeout_s=timeout, approved=False)


def gather_context(task: str, loaded: list[LoadedTool], run_id: str = "-") -> str:
    """Run the READ-ONLY tools and concatenate their labeled, bounded observations. A
    side-effecting tool is skipped here (it requires explicit approval, out of the auto path).
    A failing tool degrades to a short note -- it never breaks the run.

    Returns the NO_DATA sentinel when NOT ONE tool produced usable output (none declared, all
    write-only, all failed, all empty). That is a BLANK retrieval exactly like zero SQL rows, and
    this path fires ONCE, so the loop treats it identically: reformulate-and-retry, then escalate
    rather than score an answer written from nothing. The notes ride along as the sentinel's hint,
    so the worker still sees WHY there is no evidence."""
    ctx = _tool_context(run_id)
    blocks: list[str] = []
    usable = 0                                        # observations carrying ACTUAL evidence
    for lt in loaded:
        if not lt.tool.spec.read_only:                # least privilege: never auto-run writes
            continue
        result = dispatch(lt.tool, _render(lt.input_template, task), ctx)
        if result.ok and (result.output or "").strip():
            blocks.append(f"[{lt.label}]\n{result.output}")
            usable += 1
        elif result.ok:                                # ran fine, said nothing -> not evidence
            blocks.append(f"[{lt.label}] (no output)")
        else:
            kind = result.error.kind if result.error else "error"
            blocks.append(f"[{lt.label}] (unavailable: {kind})")
    text = "\n\n".join(blocks)
    return text if usable else no_data(text or "No tool observations.")


def describe_tools(loaded: list[LoadedTool]) -> str:
    """A short 'available tools' block for the persona prompt ({tools}). Names + read/write
    capability + description -- so the agent knows what evidence it was given."""
    if not loaded:
        return "None."
    lines = []
    for lt in loaded:
        cap = "read-only" if lt.tool.spec.read_only else "SIDE-EFFECTING (requires approval)"
        lines.append(f"- {lt.label} ({cap}): {lt.tool.spec.description}")
    return "\n".join(lines)


def _type_name(annotation) -> str:
    return getattr(annotation, "__name__", None) or str(annotation).replace("typing.", "")


def describe_tools_schema(loaded: list[LoadedTool]) -> str:
    """Like describe_tools but ALSO lists each tool's INPUT fields (from its pydantic input_model),
    so a PLANNER can form valid step inputs. A tool with no input_model accepts a free-form object.
    Used ONLY by the plan prompt (engine/tools/planned.py) -- no config, no new tool concept."""
    if not loaded:
        return "None."
    lines = []
    for lt in loaded:
        spec = lt.tool.spec
        im = spec.input_model
        if im is not None:
            try:
                fields = ", ".join(f"{n}: {_type_name(f.annotation)}"
                                   for n, f in im.model_fields.items())
            except Exception:
                fields = ""
            schema = "{" + fields + "}" if fields else "{}"
        else:
            schema = "{ free-form object }"
        cap = "read-only" if spec.read_only else "WRITE (requires approval; NOT auto-run)"
        lines.append(f"- {lt.label} [{cap}] input {schema}: {spec.description}")
    return "\n".join(lines)
