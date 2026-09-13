"""MCP adapter beyond construction: with the `mcp` SDK stubbed (no server needed), exercise the
call path -- text-content conversion (M4) and isError -> failure (H3). The stubs mimic the SDK's
async context-manager shape so McpTool._call runs under asyncio.run()."""
import sys
import types

from engine.tools import build_tool, dispatch, ToolContext


def _install_fake_mcp(monkeypatch, *, texts, is_error=False):
    class StdioServerParameters:
        def __init__(self, command=None, args=None):
            pass

    class _Result:
        def __init__(self):
            self.content = [types.SimpleNamespace(text=t) for t in texts]
            self.isError = is_error

    class ClientSession:
        def __init__(self, read, write):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def initialize(self):
            return None
        async def call_tool(self, name, arguments=None):
            return _Result()

    class _stdio_ctx:
        async def __aenter__(self):
            return (object(), object())
        async def __aexit__(self, *a):
            return False

    mcp = types.ModuleType("mcp")
    mcp.ClientSession = ClientSession
    mcp.StdioServerParameters = StdioServerParameters
    client = types.ModuleType("mcp.client")
    stdio = types.ModuleType("mcp.client.stdio")
    stdio.stdio_client = lambda server: _stdio_ctx()
    monkeypatch.setitem(sys.modules, "mcp", mcp)
    monkeypatch.setitem(sys.modules, "mcp.client", client)
    monkeypatch.setitem(sys.modules, "mcp.client.stdio", stdio)


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
