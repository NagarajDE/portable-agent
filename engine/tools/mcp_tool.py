"""
ENGINE / tools / mcp_tool -- expose ONE tool from an MCP server as a Tool, so MCP is just another
tool SOURCE behind the same ToolSpec + dispatch() boundary (no special path in the loop). The
`mcp` SDK is imported lazily; the LIVE call is UNTESTED here (no server), like GenieTool -- but its
SAFETY is enforced regardless because every call goes through dispatch() (validation, approval gate,
timeout, redaction, bounded output).

`read_only` defaults FALSE (fail-closed): an MCP tool's capability is unknown unless the pack sets
`read_only: true` (matching the server's readOnlyHint). Only read-only tools are auto-run by the
deterministic/agentic gatherers; a side-effecting MCP tool needs explicit approval.

Config (usecases/<pack>/config.yaml):
    tools:
      - type: mcp
        name: jira                      # label in observations/logs
        params:
          tool: get_issue               # the MCP tool NAME to call (required)
          command: "npx -y @modelcontextprotocol/server-jira"   # stdio server (required for now)
          read_only: true               # set true only if the tool truly does not mutate
"""
from __future__ import annotations

from engine.tools.base import ToolContext, ToolResult, ToolSpec, bound_output
from engine.tools.registry import register


class McpTool:
    def __init__(self, params: dict):
        self.spec = ToolSpec(
            name=params.get("tool_name", params.get("tool", "mcp")),
            description=params.get("description", "A tool exposed by an MCP server."),
            read_only=bool(params.get("read_only", False)),   # fail-closed: unknown capability = not auto-run
            input_model=None,                                  # MCP tools accept a free arg dict
        )
        self._tool = params.get("tool")
        self._command = params.get("command")                  # stdio server command line
        self._url = params.get("url")                          # (SSE/HTTP server -- not wired yet)
        if not self._tool:
            raise ValueError("mcp tool needs a `tool` param (the MCP tool name to call)")
        if not (self._command or self._url):
            raise ValueError("mcp tool needs a `command` (stdio) or `url` (sse) for the MCP server")

    def run(self, input: dict, ctx: ToolContext) -> ToolResult:
        import asyncio
        text = asyncio.run(self._call(dict(input or {})))
        return ToolResult(ok=True, output=bound_output(text), meta={"tool": self._tool})

    async def _call(self, arguments: dict) -> str:
        # LAZY + UNTESTED here (needs the `mcp` SDK + a live server). stdio transport only for now.
        import shlex
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        if not self._command:
            raise RuntimeError("only stdio MCP servers (`command`) are wired; set `command`")
        parts = shlex.split(self._command)
        server = StdioServerParameters(command=parts[0], args=parts[1:])
        async with stdio_client(server) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(self._tool, arguments=arguments)
                blocks = [getattr(b, "text", "") for b in (getattr(result, "content", None) or [])]
                text = "\n".join(t for t in blocks if t)
                return text or str(result)


register("mcp", McpTool)
