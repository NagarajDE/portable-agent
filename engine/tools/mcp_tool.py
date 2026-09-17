"""
ENGINE / tools / mcp_tool -- expose ONE tool from an MCP server as a Tool, so MCP is just another
tool SOURCE behind the same ToolSpec + dispatch() boundary (no special path in the loop). The
`mcp` SDK is imported lazily; the LIVE call is UNTESTED here (no server), but its SAFETY is enforced
regardless because every call goes through dispatch() (approval gate, timeout, redaction, bounded output).

Two transports, chosen by which of `command` / `url` the pack sets (exactly one):
  * LOCAL  stdio            -- `command: "npx -y @modelcontextprotocol/server-x"` (a subprocess).
  * REMOTE Streamable HTTP  -- `url: https://host/mcp` (the current MCP standard; default for a url),
    or `transport: sse` for a legacy SSE server.

Remote safety (mirrors http_tool): **https only**, no userinfo in the URL, the host must be on an
ALLOWLIST (pack `allow_hosts:`; unset -> MCP_ALLOWED_HOSTS env; empty -> deny), the host must not resolve
to a private/loopback address (re-checked right before each connect), and the bearer token comes from an
env var NAMED in config (`token_env`) -- never the token itself in a pack. The token is only ever sent
over https to the configured origin (the SDK client is created per call; it never follows a redirect
with credentials because the URL is fixed).

Arguments are VALIDATED against the server's own per-tool JSON schema (`list_tools` discovery, cached
after the first call) before `call_tool` -- a malformed call fails as a normalized `validation` error
instead of reaching the server. Non-text content blocks (image / audio / resource) are surfaced as
labeled markers; `structuredContent` is surfaced as JSON when there is no text.

`read_only` defaults FALSE (fail-closed): an MCP tool's capability is unknown unless the pack sets
`read_only: true` (matching the server's readOnlyHint). Only read-only tools are auto-run by the
deterministic/agentic/planned gatherers; a side-effecting MCP tool needs explicit approval.

Config (usecases/<pack>/config.yaml):
    tools:
      - type: mcp
        name: jira                      # label in observations/logs
        params:
          tool: get_issue               # the MCP tool NAME to call (required)
          command: "npx -y @modelcontextprotocol/server-jira"   # LOCAL stdio server ...
          # url: https://mcp.example.com/mcp                      # ... OR a REMOTE server (https only)
          # transport: streamable_http   # | sse   (remote only; default streamable_http)
          # allow_hosts: [mcp.example.com]   # remote: required (or MCP_ALLOWED_HOSTS env)
          # token_env: JIRA_MCP_TOKEN        # NAME of the env var holding the bearer token (never the value)
          read_only: true               # set true only if the tool truly does not mutate
"""
from __future__ import annotations

import json
import os
from urllib.parse import urlsplit

from engine.tools.base import ToolContext, ToolResult, ToolSpec, bound_output, strict_bool
from engine.tools.dispatch import ToolValidationError
from engine.tools.http_tool import _coerce_allow, host_of, is_host_allowed, resolves_to_blocked_ip
from engine.tools.registry import register

_TRANSPORTS = ("streamable_http", "sse")


def _env_allowlist() -> list[str]:
    raw = os.getenv("MCP_ALLOWED_HOSTS", "")
    return [h.strip() for h in raw.split(",") if h.strip()]


def validate_remote_url(url: str, allowlist) -> str:
    """Pure (no DNS) checks for a remote MCP URL: https only, no userinfo, allowlisted host."""
    u = urlsplit(url or "")
    if u.scheme != "https":
        raise ValueError("mcp `url` must be https (a token must never travel over http)")
    if "@" in (u.netloc or ""):
        raise ValueError("mcp `url` must not contain userinfo")
    host = host_of(url)
    if not host or not is_host_allowed(host, allowlist):
        raise ValueError(f"mcp host {host!r} not in allowlist {sorted(allowlist)} "
                         f"(set the tool's allow_hosts or MCP_ALLOWED_HOSTS)")
    return url


# --- a small JSON-Schema checker (the subset MCP tool schemas actually use) --------------------------------
_TYPES = {"object": (dict,), "array": (list,), "string": (str,), "boolean": (bool,), "null": (type(None),)}


def _is_type(value, t: str) -> bool:
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, _TYPES.get(t, (object,)))


def check_schema(value, schema, path: str = "args") -> None:
    """Validate `value` against a JSON-Schema dict: type (incl. a type list), enum, required,
    properties, additionalProperties:false, items. Raises ToolValidationError with a VALUE-FREE message
    (field paths and expected types only -- a bad argument may carry a secret). Unknown keywords are
    ignored (permissive where the schema is richer than this checker; the server still validates)."""
    if not isinstance(schema, dict):
        return
    t = schema.get("type")
    types = t if isinstance(t, list) else ([t] if isinstance(t, str) else [])
    if types and not any(_is_type(value, x) for x in types):
        raise ToolValidationError(f"{path}: expected {'/'.join(types)}, got {type(value).__name__}")
    if "enum" in schema and value not in (schema.get("enum") or []):
        raise ToolValidationError(f"{path}: not one of the allowed values")
    if isinstance(value, dict):
        props = schema.get("properties") or {}
        missing = [r for r in (schema.get("required") or []) if r not in value]
        if missing:
            raise ToolValidationError(f"{path}: missing required field(s) {missing}")
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - set(props))
            if extra:
                raise ToolValidationError(f"{path}: unknown field(s) {extra}")
        for k, v in value.items():
            if k in props:
                check_schema(v, props[k], f"{path}.{k}")
    elif isinstance(value, list) and isinstance(schema.get("items"), dict):
        for i, v in enumerate(value):
            check_schema(v, schema["items"], f"{path}[{i}]")


def _content_text(result) -> str:
    """Flatten a CallToolResult's content: text blocks verbatim; image/audio/resource blocks as labeled
    markers (never dropped silently); structuredContent as JSON when no text was returned."""
    parts = []
    for b in (getattr(result, "content", None) or []):
        kind = str(getattr(b, "type", "") or "")
        text = getattr(b, "text", None)
        if kind in ("", "text") and text:
            parts.append(str(text))
        elif kind == "image" or kind == "audio":
            parts.append(f"[{kind}: {getattr(b, 'mimeType', 'binary')}]")
        elif kind in ("resource", "resource_link"):
            res = getattr(b, "resource", None) or b
            inner = getattr(res, "text", None)
            parts.append(str(inner) if inner else f"[resource: {getattr(res, 'uri', '?')}]")
        elif text:
            parts.append(str(text))
    if not parts:
        sc = getattr(result, "structuredContent", None)
        if sc:
            parts.append(json.dumps(sc, default=str))
    return "\n".join(parts)


class McpTool:
    def __init__(self, params: dict):
        self.spec = ToolSpec(
            name=params.get("tool_name", params.get("tool", "mcp")),
            description=params.get("description", "A tool exposed by an MCP server."),
            read_only=strict_bool(params.get("read_only"), False),   # fail-closed + strict (H4)
            input_model=None,                                  # validated against the SERVER's schema instead
        )
        self._tool = params.get("tool")
        self._command = (params.get("command") or "").strip() or None       # stdio server command line
        self._url = (params.get("url") or "").strip() or None               # remote server
        self._token_env = params.get("token_env")                           # NAME of an env var, never the token
        self._schema = None                                                 # discovered per-tool inputSchema
        if not self._tool:
            raise ValueError("mcp tool needs a `tool` param (the MCP tool name to call)")
        # fail FAST at construction, not at first call
        if bool(self._command) == bool(self._url):
            raise ValueError("mcp tool needs exactly ONE of `command` (local stdio) or `url` (remote https)")
        self._transport = str(params.get("transport") or "streamable_http").strip().lower()
        if self._url:
            if self._transport not in _TRANSPORTS:
                raise ValueError(f"mcp transport must be one of {_TRANSPORTS}, got {self._transport!r}")
            allow = params.get("allow_hosts")
            self._allow = _coerce_allow(allow) if allow is not None else _env_allowlist()
            validate_remote_url(self._url, self._allow)
        else:
            self._transport, self._allow = "stdio", []

    def run(self, input: dict, ctx: ToolContext) -> ToolResult:
        import asyncio
        text = asyncio.run(self._call(dict(input or {}), max(1.0, float(ctx.timeout_s))))
        return ToolResult(ok=True, output=bound_output(text),
                          meta={"tool": self._tool, "transport": self._transport})

    # --- transport ------------------------------------------------------------------------------------
    def _headers(self) -> dict:
        h = {"User-Agent": "portable-agent"}
        token = os.getenv(self._token_env) if self._token_env else None
        if token:
            h["Authorization"] = f"Bearer {token}"                     # https is guaranteed by validate_remote_url
        return h

    async def _call(self, arguments: dict, timeout_s: float) -> str:
        # LAZY + UNTESTED against a live server (needs the `mcp` SDK + a server).
        from mcp import ClientSession
        if self._command:
            import shlex
            from mcp import StdioServerParameters
            from mcp.client.stdio import stdio_client
            parts = shlex.split(self._command)
            server = StdioServerParameters(command=parts[0], args=parts[1:])
            async with stdio_client(server) as (read, write):
                return await self._session(ClientSession, read, write, arguments)
        # REMOTE: re-validate (incl. DNS -> private address) right before every connect (like http_tool)
        validate_remote_url(self._url, self._allow)
        if resolves_to_blocked_ip(host_of(self._url)):
            raise ValueError(f"mcp host {host_of(self._url)!r} resolves to a blocked (non-public) address")
        headers = self._headers()
        if self._transport == "sse":
            from mcp.client.sse import sse_client
            async with sse_client(self._url, headers=headers, timeout=timeout_s,
                                  sse_read_timeout=timeout_s) as (read, write):
                return await self._session(ClientSession, read, write, arguments)
        from datetime import timedelta
        from mcp.client.streamable_http import streamablehttp_client
        async with streamablehttp_client(self._url, headers=headers, timeout=timedelta(seconds=timeout_s),
                                         sse_read_timeout=timedelta(seconds=timeout_s)) as (read, write, _):
            return await self._session(ClientSession, read, write, arguments)

    async def _session(self, ClientSession, read, write, arguments: dict) -> str:
        async with ClientSession(read, write) as session:
            await session.initialize()
            await self._validate_args(session, arguments)
            result = await session.call_tool(self._tool, arguments=arguments)
            text = _content_text(result)
            if getattr(result, "isError", False):          # H3: a tool-level error is NOT success;
                raise RuntimeError(                         # raise -> dispatch normalizes to ok=False
                    f"MCP tool {self._tool!r} returned an error: {text or result!r}")
            return text or str(result)

    async def _validate_args(self, session, arguments: dict) -> None:
        """Discover the tool's inputSchema once (list_tools) and check the arguments against it."""
        if self._schema is None:
            listed = await session.list_tools()
            tools = list(getattr(listed, "tools", None) or [])
            match = next((t for t in tools if getattr(t, "name", None) == self._tool), None)
            if match is None:
                names = sorted(str(getattr(t, "name", "?")) for t in tools)[:20]
                raise ValueError(f"MCP server exposes no tool {self._tool!r}; available: {names}")
            self._schema = getattr(match, "inputSchema", None) or {}
        check_schema(arguments, self._schema)


register("mcp", McpTool)
