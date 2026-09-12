"""
ENGINE / tools / agentic -- an OPT-IN, bounded ReAct-style CONTEXT-GATHERING loop. The model
chooses which READ-ONLY tools to call (unlike the deterministic orchestrator, where the engine
picks); the engine dispatches each choice through the SAME dispatch() trust boundary and feeds the
observations back. When the model says {"final": true} (or the step budget is hit), gathering stops
and the normal `generate` node writes the answer from the accumulated observations.

Design choices (minimal + safe):
  * PORTABLE tool-calling -- the model emits a JSON object in TEXT ({"tool": ..., "input": ...} or
    {"final": true}); we parse it. No change to the LLMClient Protocol, so it works on ANY provider
    (and on the mock). Native function-calling can be added later behind this same function.
  * READ-ONLY only -- ctx.approved is False, and a side-effecting tool is never auto-run (it returns
    an observation saying approval is required). Writes stay out of the auto path, as in the
    deterministic gatherer.
  * BOUNDED -- at most `max_steps` tool calls; identical (tool,input) calls are de-duplicated; a
    worker error during gathering is non-fatal (we use whatever evidence we have).
  * TRUST TRADE-OFF (documented): once the model picks tools from observations, tool output CAN
    influence later tool selection -- weaker than the deterministic path's injection-safety. Every
    call still goes through dispatch() (validation, timeout, redaction, bounds); http stays
    allowlisted. Packs opt in via `tool_mode: agentic`.
"""
from __future__ import annotations

import json
import os

from engine.tools.base import ToolContext
from engine.tools.dispatch import dispatch
from engine.tools.orchestrator import describe_tools


def _first_json_object(text: str) -> dict | None:
    """Parse the FIRST balanced {...} object in text (tracking JSON strings + escapes) into a dict,
    or None. Self-contained (no import from engine.graph) to avoid a graph<->tools cycle."""
    if not text:
        return None
    start = text.find("{")
    if start == -1:
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:            esc = False
            elif c == "\\":    esc = True
            elif c == '"':     in_str = False
            continue
        if c == '"':           in_str = True
        elif c == "{":         depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(text[start:i + 1])
                except (ValueError, TypeError):
                    return None
                return obj if isinstance(obj, dict) else None
    return None


def _render(template: str, task: str, tools: str, observations: str) -> str:
    """Fill the three act-prompt placeholders by plain substitution (no str.format -- tool output
    may contain braces). Kept local so this module doesn't import engine.graph.fill()."""
    return (template.replace("{task}", task)
                    .replace("{tools}", tools)
                    .replace("{observations}", observations))


def run_agentic(task: str, loaded, llm, act_template: str,
                run_id: str = "-", max_steps: int = 4) -> str:
    """Model-driven, bounded gathering over a pack's READ-ONLY tools. Returns the concatenated,
    labeled observations (the {data}/{observations} the `generate` node then reasons over)."""
    timeout = max(1.0, float(os.getenv("TOOL_TIMEOUT_SECONDS", "30")))
    ctx = ToolContext(run_id=run_id or "-", timeout_s=timeout, approved=False)   # never auto-approve writes
    by_name = {lt.label: lt for lt in loaded}
    observations: list[str] = []
    seen: set = set()
    for _ in range(max(0, int(max_steps))):
        rendered = _render(act_template, task, describe_tools(loaded),
                           "\n\n".join(observations) or "None yet.")
        try:
            raw = llm.complete(rendered)
        except Exception:
            break                                    # a worker error during gathering is non-fatal
        obj = _first_json_object(raw)
        if not isinstance(obj, dict) or obj.get("final") or "tool" not in obj:
            break                                    # model is done gathering (or unparseable)
        name = str(obj.get("tool", ""))
        lt = by_name.get(name)
        if lt is None:
            observations.append(f"[{name}] (no such tool)")
            continue
        if not lt.tool.spec.read_only:               # least privilege: never auto-run a write
            observations.append(f"[{name}] (side-effecting; requires approval — not run)")
            continue
        tool_input = obj.get("input") if isinstance(obj.get("input"), dict) else {}
        key = (name, json.dumps(tool_input, sort_keys=True, default=str))
        if key in seen:
            observations.append(f"[{name}] (already called with same input; skipped)")
            continue
        seen.add(key)
        result = dispatch(lt.tool, tool_input, ctx)  # SAME trust boundary as everything else
        if result.ok:
            observations.append(f"[{name}]\n{result.output}")
        else:
            kind = result.error.kind if result.error else "error"
            observations.append(f"[{name}] (unavailable: {kind})")
    return "\n\n".join(observations) if observations else "No tool observations."
