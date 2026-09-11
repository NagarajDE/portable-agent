"""
ENGINE / tools / mock_tool -- a deterministic, in-process tool. Zero deps, zero network,
zero credentials. This is the generic stand-in used by example packs and by tests to
exercise the tool machinery without touching anything external (parallels MockClient /
MockSQLTool).

Params (all optional):
  tool_name    label used in the spec/observations (default "mock")
  description  spec description
  read_only    default True. Set False to model a SIDE-EFFECTING tool (used by tests to
               prove the approval gate blocks it) -- real adapters hardcode this in code.
  output       fixed text to return; if omitted, echoes the (validated) input for tests
  responses    dict mapping a lookup key -> text; combined with `key_from` input field
  key_from     which input field to look up in `responses` (default "query")
  fail         if set, run() raises RuntimeError(fail) -- to exercise error normalization
  delay_s      sleep this long before returning -- to exercise the dispatch timeout
"""
from __future__ import annotations

import time

from engine.tools.base import ToolContext, ToolResult, ToolSpec
from engine.tools.registry import register


class MockTool:
    def __init__(self, params: dict):
        self.spec = ToolSpec(
            name=params.get("tool_name", "mock"),
            description=params.get("description", "Deterministic in-process mock tool."),
            read_only=bool(params.get("read_only", True)),
            input_model=None,                        # accepts a free dict (kept generic)
        )
        self._output = params.get("output")
        self._responses = params.get("responses") or {}
        self._key_from = params.get("key_from", "query")
        self._fail = params.get("fail")
        self._delay_s = float(params.get("delay_s", 0) or 0)

    def run(self, input: dict, ctx: ToolContext) -> ToolResult:
        if self._delay_s:
            time.sleep(self._delay_s)                # let a test trip the timeout
        if self._fail:
            raise RuntimeError(str(self._fail))      # dispatch normalizes this into a ToolResult
        if self._responses:
            key = str(input.get(self._key_from, ""))
            return ToolResult(ok=True, output=str(self._responses.get(key, "No match.")),
                              meta={"matched_key": key})
        if self._output is not None:
            return ToolResult(ok=True, output=str(self._output), meta={"echo_input": input})
        return ToolResult(ok=True, output=f"mock observation for input={input}",
                          meta={"echo_input": input})


register("mock", MockTool)
