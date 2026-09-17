"""MCP adapter beyond construction: with the `mcp` SDK stubbed (no server needed), exercise the
call path -- text-content conversion (M4), isError -> failure (H3), the REMOTE transports (R3:
streamable_http / sse, https-only + allowlist + env-only bearer token), and per-tool argument
validation against the server's schema (R4). The stubs mimic the SDK's async context-manager shape
so McpTool._call runs under asyncio.run()."""
import sys
import types

import pytest

import engine.tools.mcp_tool as M
from engine.tools import build_tool, dispatch, ToolContext
from engine.tools.mcp_tool import check_schema, validate_remote_url
from engine.tools.dispatch import ToolValidationError


def _install_fake_mcp(monkeypatch, *, texts=(), is_error=False, tools=None, blocks=None,
                      structured=None, task_group=False):
    """A fake `mcp` SDK: stdio + streamable_http + sse transports, list_tools discovery (`tools` =
    {name: inputSchema}), and a CallToolResult with text (or arbitrary `blocks`) content. Records the
    transport used and the arguments sent in `calls`. `task_group=True` mimics the real SDK's anyio task
    groups: any error leaving a transport/session context is wrapped in an ExceptionGroup."""
    calls = {"transport": None, "url": None, "headers": None, "args": None, "list_tools": 0}

    def _wrap(ev):
        if task_group and ev is not None:
            raise ExceptionGroup("unhandled errors in a TaskGroup (1 sub-exception)", [ev])

    class StdioServerParameters:
        def __init__(self, command=None, args=None):
            pass

    class _Result:
        def __init__(self):
            self.content = list(blocks) if blocks is not None else [
                types.SimpleNamespace(type="text", text=t) for t in texts]
            self.isError = is_error
            self.structuredContent = structured

    class ClientSession:
        def __init__(self, read, write):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, et, ev, tb):
            _wrap(ev)
            return False
        async def initialize(self):
            return None
        async def list_tools(self):
            calls["list_tools"] += 1
            spec = tools if tools is not None else {"get_issue": {}, "do_thing": {}}
            return types.SimpleNamespace(tools=[types.SimpleNamespace(name=n, inputSchema=s)
                                               for n, s in spec.items()])
        async def call_tool(self, name, arguments=None):
            calls["args"] = arguments
            return _Result()

    class _ctx:
        def __init__(self, n):
            self.n = n
        async def __aenter__(self):
            return tuple(object() for _ in range(self.n))
        async def __aexit__(self, et, ev, tb):
            _wrap(ev)                                   # nested: session group inside the transport group
            return False

    def _stdio(server):
        calls["transport"] = "stdio"
        return _ctx(2)

    def _http(url, headers=None, timeout=None, sse_read_timeout=None, **k):
        calls.update(transport="streamable_http", url=url, headers=headers)
        return _ctx(3)

    def _sse(url, headers=None, timeout=None, sse_read_timeout=None, **k):
        calls.update(transport="sse", url=url, headers=headers)
        return _ctx(2)

    mcp = types.ModuleType("mcp")
    mcp.ClientSession = ClientSession
    mcp.StdioServerParameters = StdioServerParameters
    client = types.ModuleType("mcp.client")
    stdio = types.ModuleType("mcp.client.stdio")
    stdio.stdio_client = _stdio
    http = types.ModuleType("mcp.client.streamable_http")
    http.streamablehttp_client = _http
    sse = types.ModuleType("mcp.client.sse")
    sse.sse_client = _sse
    for name, mod in (("mcp", mcp), ("mcp.client", client), ("mcp.client.stdio", stdio),
                      ("mcp.client.streamable_http", http), ("mcp.client.sse", sse)):
        monkeypatch.setitem(sys.modules, name, mod)
    return calls


def test_mcp_text_content_conversion(monkeypatch):
    _install_fake_mcp(monkeypatch, texts=["hello", "world"])
    t = build_tool("mcp", {"tool": "get_issue", "command": "srv", "read_only": True})
    r = t.run({"id": 1}, ToolContext())
    # EXACT joined text (M14): substring checks would pass even on a stringified SDK object -- assert
    # the precise "\n".join of the text blocks, proving real content extraction.
    assert r.ok and r.output == "hello\nworld"


def test_mcp_iserror_becomes_failure(monkeypatch):
    # H3: CallToolResult(isError=True) must NOT be reported as success. Through dispatch (the tool is
    # side-effecting by default -> approve so it runs), we get a normalized ok=False.
    _install_fake_mcp(monkeypatch, texts=["boom"], is_error=True)
    t = build_tool("mcp", {"tool": "do_thing", "command": "srv"})   # read_only defaults False
    res = dispatch(t, {"x": 1}, ToolContext(approved=True))
    assert res.ok is False and res.error is not None


# --- R3: remote transports -------------------------------------------------------------------------------------
_REMOTE = {"tool": "get_issue", "url": "https://mcp.example.com/mcp", "allow_hosts": ["mcp.example.com"],
           "read_only": True, "token_env": "JIRA_MCP_TOKEN"}


@pytest.fixture
def _public_dns(monkeypatch):
    monkeypatch.setattr(M, "resolves_to_blocked_ip", lambda host: False)   # no DNS in unit tests


def test_remote_streamable_http_is_the_default_and_sends_the_env_token(monkeypatch, _public_dns):
    calls = _install_fake_mcp(monkeypatch, texts=["issue-1"])
    monkeypatch.setenv("JIRA_MCP_TOKEN", "tok-123")
    t = build_tool("mcp", dict(_REMOTE))
    res = dispatch(t, {}, ToolContext())
    assert res.ok and res.output == "issue-1" and res.meta["transport"] == "streamable_http"
    assert calls["transport"] == "streamable_http" and calls["url"] == "https://mcp.example.com/mcp"
    assert calls["headers"]["Authorization"] == "Bearer tok-123"


def test_remote_sse_transport(monkeypatch, _public_dns):
    calls = _install_fake_mcp(monkeypatch, texts=["ok"])
    t = build_tool("mcp", {**_REMOTE, "transport": "sse"})
    assert dispatch(t, {}, ToolContext()).ok and calls["transport"] == "sse"
    assert "Authorization" not in calls["headers"]                       # token env unset -> no header


def test_remote_stdio_still_works(monkeypatch):
    calls = _install_fake_mcp(monkeypatch, texts=["ok"])
    t = build_tool("mcp", {"tool": "get_issue", "command": "srv", "read_only": True})
    assert dispatch(t, {}, ToolContext()).ok and calls["transport"] == "stdio"


@pytest.mark.parametrize("params, msg", [
    ({"tool": "t", "command": "srv", "url": "https://mcp.example.com/mcp"}, "exactly ONE"),
    ({"tool": "t"}, "exactly ONE"),
    ({"tool": "t", "url": "http://mcp.example.com/mcp", "allow_hosts": ["mcp.example.com"]}, "https"),
    ({"tool": "t", "url": "https://u:p@mcp.example.com/mcp", "allow_hosts": ["mcp.example.com"]}, "userinfo"),
    ({"tool": "t", "url": "https://evil.example.com/mcp", "allow_hosts": ["mcp.example.com"]}, "allowlist"),
    ({"tool": "t", "url": "https://mcp.example.com/mcp"}, "allowlist"),          # no allowlist at all -> deny
    ({"tool": "t", "url": "https://mcp.example.com/mcp", "allow_hosts": []}, "allowlist"),
    ({"tool": "t", "url": "https://mcp.example.com/mcp", "allow_hosts": ["mcp.example.com"],
      "transport": "websocket"}, "transport"),
])
def test_remote_config_fails_fast_at_construction(params, msg):
    with pytest.raises(ValueError, match=msg):
        build_tool("mcp", params)


def test_env_allowlist_is_the_fallback(monkeypatch):
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "other.example.com, mcp.example.com")
    build_tool("mcp", {"tool": "t", "url": "https://mcp.example.com/mcp"})   # constructs
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "other.example.com")
    with pytest.raises(ValueError):
        build_tool("mcp", {"tool": "t", "url": "https://mcp.example.com/mcp"})


def test_remote_blocked_address_is_refused_before_connect(monkeypatch):
    calls = _install_fake_mcp(monkeypatch, texts=["ok"])
    monkeypatch.setattr(M, "resolves_to_blocked_ip", lambda host: True)      # e.g. resolves to 10.0.0.5
    res = dispatch(build_tool("mcp", dict(_REMOTE)), {}, ToolContext())
    assert not res.ok and "blocked" in res.error.message and calls["transport"] is None   # never connected


def test_validate_remote_url_is_pure():
    assert validate_remote_url("https://a.example.com/x", ["example.com"])   # dot-suffix match
    with pytest.raises(ValueError):
        validate_remote_url("https://a.example.com/x", ["b.example.com"])


# --- R4: arguments validated against the server's per-tool schema --------------------------------------------
_SCHEMA = {"type": "object", "required": ["id"], "additionalProperties": False,
           "properties": {"id": {"type": "integer"}, "mode": {"type": "string", "enum": ["a", "b"]},
                          "tags": {"type": "array", "items": {"type": "string"}}}}


@pytest.mark.parametrize("args", [{}, {"id": "x"}, {"id": 1, "extra": 1}, {"id": 1, "mode": "z"},
                                  {"id": 1, "tags": [1]}, {"id": True}])
def test_bad_args_fail_as_validation_before_the_server_is_called(monkeypatch, args):
    calls = _install_fake_mcp(monkeypatch, texts=["ok"], tools={"get_issue": _SCHEMA})
    res = dispatch(build_tool("mcp", {"tool": "get_issue", "command": "srv", "read_only": True}), args,
                   ToolContext())
    assert not res.ok and res.error.kind == "validation" and calls["args"] is None


def test_good_args_reach_the_server_and_the_schema_is_cached(monkeypatch):
    calls = _install_fake_mcp(monkeypatch, texts=["ok"], tools={"get_issue": _SCHEMA})
    t = build_tool("mcp", {"tool": "get_issue", "command": "srv", "read_only": True})
    assert dispatch(t, {"id": 1, "mode": "a", "tags": ["x"]}, ToolContext()).ok
    assert dispatch(t, {"id": 2}, ToolContext()).ok
    assert calls["args"] == {"id": 2} and calls["list_tools"] == 1        # discovered once, reused


def test_unknown_tool_name_lists_what_the_server_offers(monkeypatch):
    _install_fake_mcp(monkeypatch, texts=["ok"], tools={"search": {}, "get_issue": {}})
    res = dispatch(build_tool("mcp", {"tool": "nope", "command": "srv", "read_only": True}), {}, ToolContext())
    assert not res.ok and "get_issue" in res.error.message and "search" in res.error.message


def test_validation_messages_never_echo_values():
    with pytest.raises(ToolValidationError) as e:
        check_schema({"id": "HUNTER2SECRET"}, _SCHEMA)
    assert "HUNTER2SECRET" not in str(e.value) and "args.id" in str(e.value)
    check_schema({"id": 1}, {"type": ["object", "null"]})                   # a type list
    check_schema(None, {"type": ["object", "null"]})
    check_schema({"anything": 1}, {})                                        # no schema -> permissive


# --- BUG 2 (live): errors raised inside the SDK's task groups must surface as their REAL cause ----------------
def test_validation_error_inside_task_group_is_still_kind_validation(monkeypatch):
    calls = _install_fake_mcp(monkeypatch, texts=["ok"], tools={"get_issue": _SCHEMA}, task_group=True)
    res = dispatch(build_tool("mcp", {"tool": "get_issue", "command": "srv", "read_only": True}), {"id": "x"},
                   ToolContext())
    assert not res.ok and res.error.kind == "validation"
    assert "args.id" in res.error.message and "TaskGroup" not in res.error.message
    assert calls["args"] is None                                         # never reached the server


def test_tool_error_inside_task_group_shows_innermost_cause(monkeypatch):
    _install_fake_mcp(monkeypatch, texts=["boom"], is_error=True, task_group=True)
    res = dispatch(build_tool("mcp", {"tool": "do_thing", "command": "srv"}), {}, ToolContext(approved=True))
    assert not res.ok and res.error.kind == "runtime"
    assert "returned an error: boom" in res.error.message and "ExceptionGroup" not in res.error.message


def test_unknown_tool_inside_task_group_shows_innermost_cause(monkeypatch):
    _install_fake_mcp(monkeypatch, texts=["ok"], tools={"search": {}}, task_group=True)
    res = dispatch(build_tool("mcp", {"tool": "nope", "command": "srv", "read_only": True}), {}, ToolContext())
    assert not res.ok and "exposes no tool 'nope'" in res.error.message and "TaskGroup" not in res.error.message


def test_unwrap_error_prefers_validation_then_ordinary_exceptions():
    import asyncio
    from engine.tools.mcp_tool import unwrap_error
    v, r, c = ToolValidationError("args.id: bad"), RuntimeError("real"), asyncio.CancelledError()
    nested = BaseExceptionGroup("outer", [BaseExceptionGroup("inner", [c, r]), v])   # CancelledError is a BaseException
    assert unwrap_error(nested) is v
    assert unwrap_error(BaseExceptionGroup("g", [c, r])) is r
    assert unwrap_error(r) is r                                          # a plain exception passes through


def test_missing_mcp_sdk_is_an_actionable_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "mcp", None)                        # simulate `pip install mcp` not done
    res = dispatch(build_tool("mcp", {"tool": "t", "command": "srv", "read_only": True}), {}, ToolContext())
    assert not res.ok and "pip install mcp" in res.error.message and "type: mcp" in res.error.message


# --- non-text content blocks are surfaced, not dropped ----------------------------------------------------------
def test_non_text_blocks_and_structured_content_are_surfaced(monkeypatch):
    blocks = [types.SimpleNamespace(type="text", text="hello"),
              types.SimpleNamespace(type="image", mimeType="image/png", text=None),
              types.SimpleNamespace(type="resource", text=None,
                                    resource=types.SimpleNamespace(uri="file:///r.txt", text="embedded")),
              types.SimpleNamespace(type="resource_link", text=None, uri="https://x/y", resource=None)]
    _install_fake_mcp(monkeypatch, blocks=blocks)
    t = build_tool("mcp", {"tool": "get_issue", "command": "srv", "read_only": True})
    out = t.run({}, ToolContext()).output
    assert out == "hello\n[image: image/png]\nembedded\n[resource: https://x/y]"
    _install_fake_mcp(monkeypatch, blocks=[], structured={"total": 3})
    t = build_tool("mcp", {"tool": "get_issue", "command": "srv", "read_only": True})
    assert t.run({}, ToolContext()).output == '{"total": 3}'
