"""Exercise the REAL, pinned `mcp` SDK end to end -- offline. A genuine FastMCP server runs as a stdio
subprocess and is called through McpTool + dispatch(), so a pin bump is proven against the actual client API,
the actual anyio task-group behaviour (the ExceptionGroup unwrap) and a real `list_tools` inputSchema --
not the stubs in tests/test_tools_mcp.py. Skips only when the `mcp` package is not installed.

No network: the transport is stdio (a child process). Each dispatch spawns its own short-lived server."""
import inspect
import sys
import textwrap

import pytest

pytest.importorskip("mcp")
pytest.importorskip("mcp.server.fastmcp")

from engine.tools import build_tool, dispatch, ToolContext

_SERVER = textwrap.dedent('''
    from mcp.server.fastmcp import FastMCP
    app = FastMCP("pa-test")

    @app.tool()
    def echo(text: str) -> str:
        """Echo the text back."""
        return "echo:" + text

    @app.tool()
    def fail(reason: str) -> str:
        """Always fails, on purpose."""
        raise ValueError("failed on purpose: " + reason)

    app.run(transport="stdio")
''')


@pytest.fixture(scope="module")
def server_cmd(tmp_path_factory):
    p = tmp_path_factory.mktemp("mcp") / "server.py"
    p.write_text(_SERVER, encoding="utf-8")
    # forward slashes + quotes: McpTool splits the command with shlex (POSIX), where a backslash escapes
    return f'"{sys.executable.replace(chr(92), "/")}" "{p.as_posix()}"'


def _tool(cmd, name):
    return build_tool("mcp", {"tool": name, "command": cmd, "read_only": True})


def _ctx():
    return ToolContext(run_id="real-sdk", timeout_s=90)          # a cold python + FastMCP start on Windows


def test_real_stdio_round_trip(server_cmd):
    res = dispatch(_tool(server_cmd, "echo"), {"text": "hi"}, _ctx())
    assert res.ok, res.error
    assert res.output == "echo:hi" and res.meta["transport"] == "stdio"


def test_real_schema_validation_uses_the_servers_input_schema(server_cmd):
    t = _tool(server_cmd, "echo")
    bad = dispatch(t, {"text": 5}, _ctx())                          # FastMCP declares text: string
    assert not bad.ok and bad.error.kind == "validation"
    assert "args.text" in bad.error.message and "TaskGroup" not in bad.error.message
    missing = dispatch(t, {}, _ctx())
    assert not missing.ok and missing.error.kind == "validation" and "text" in missing.error.message


def test_real_unknown_tool_lists_the_servers_tools(server_cmd):
    res = dispatch(_tool(server_cmd, "nope"), {}, _ctx())
    assert not res.ok and "echo" in res.error.message and "fail" in res.error.message
    assert "TaskGroup" not in res.error.message


def test_real_tool_error_surfaces_its_own_message(server_cmd):
    res = dispatch(_tool(server_cmd, "fail"), {"reason": "x"}, _ctx())
    assert not res.ok and res.error.kind == "runtime"
    assert "failed on purpose: x" in res.error.message              # the SDK's isError text, not the wrapper
    assert "TaskGroup" not in res.error.message and "ExceptionGroup" not in res.error.message


def test_real_remote_client_signatures_match_what_we_pass():
    """The remote transports can't be exercised offline (https + allowlist by design), so pin the API shape."""
    from mcp.client.sse import sse_client
    from mcp.client.streamable_http import streamablehttp_client
    for fn in (streamablehttp_client, sse_client):
        params = inspect.signature(fn).parameters
        assert {"url", "headers", "timeout", "sse_read_timeout"} <= set(params), fn.__name__
