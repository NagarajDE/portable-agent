"""LIVE smokes -- one per feature (R1 non-loop, R2 Genie, R3 remote MCP). SKIPPED by default: they need real
credentials and are run BY HAND, never in CI. Opt in with LIVE_SMOKE=1 plus the feature's own env; the
offline suite (`pytest -q`) never touches these.

    LIVE_SMOKE=1  LIVE_PACK=<pack>                                py -3 -m pytest tests/live -q -s
    LIVE_SMOKE=1  LIVE_PACK=<databricks pack>  SQL_TOOL=genie     ... (DATABRICKS_HOST/TOKEN set; pack has a
                                                                       databricks: {genie_space} block)
    LIVE_SMOKE=1  LIVE_MCP_URL=https://host/mcp  LIVE_MCP_TOOL=<name>  LIVE_MCP_TOKEN_ENV=<ENV_NAME>
                  [LIVE_MCP_ARGS='{"k": "v"}']                     ... (MCP_ALLOWED_HOSTS or the host is used)
"""
import json
import os

import pytest

pytestmark = pytest.mark.skipif(os.getenv("LIVE_SMOKE") != "1", reason="live smoke: set LIVE_SMOKE=1 + creds")


def _need(*names):
    missing = [n for n in names if not os.getenv(n)]
    if missing:
        pytest.skip(f"live smoke needs env {missing}")


def test_live_non_loop_run_is_grounded_and_unscored():
    """R1: any real-worker pack, forced NON-LOOP -> one worker answer, status ok, best_score -1."""
    _need("LIVE_PACK", "WORKER_PROVIDER")
    import engine.graph as G
    from engine.graph import build_graph, initial_state
    from engine.tracing import traced_invoke
    uc = os.environ["LIVE_PACK"]
    real = G.load_config
    G.load_config = lambda u: {**real(u), "loop": False}
    try:
        cfg = real(uc)
        final = traced_invoke(build_graph(uc), initial_state(cfg["sample_task"]), uc)
    finally:
        G.load_config = real
    print("\nSTATUS:", final["status"] or "ok", "| SCORE:", final["best_score"], "\nANSWER:", final["best_answer"])
    assert final["best_score"] == -1 and final["best_answer"]
    assert final["status"] in ("", "no_data", "out_of_scope")


def test_live_genie_returns_grounded_rows():
    """R2: SQL_TOOL=genie on a pack whose semantic_layer.yaml has databricks: {genie_space: ...}."""
    _need("LIVE_PACK", "DATABRICKS_HOST", "DATABRICKS_TOKEN")
    if (os.getenv("SQL_TOOL") or "").lower() != "genie":
        pytest.skip("set SQL_TOOL=genie")
    from engine.graph import load_semantic_layer
    from engine.sql_tool import get_sql_tool, is_no_data
    uc = os.environ["LIVE_PACK"]
    tool = get_sql_tool(uc, "genie", load_semantic_layer(uc))
    q = os.getenv("LIVE_QUESTION") or "How many rows are there in total?"
    out = tool.ask(q)
    print("\nSQL:", tool.last_sql, "\nROWS:\n", out[:800])
    assert tool.last_sql.upper().lstrip().startswith(("SELECT", "WITH")) or is_no_data(out)
    assert out.strip()


def test_live_remote_mcp_call_through_dispatch():
    """R3: a remote MCP tool completes a call through dispatch() (validation, timeout, redaction, bounds)."""
    _need("LIVE_MCP_URL", "LIVE_MCP_TOOL")
    from urllib.parse import urlsplit
    from engine.tools import build_tool, dispatch, ToolContext
    url = os.environ["LIVE_MCP_URL"]
    params = {"tool": os.environ["LIVE_MCP_TOOL"], "url": url, "read_only": True,
              "allow_hosts": [urlsplit(url).hostname], "transport": os.getenv("LIVE_MCP_TRANSPORT", "streamable_http")}
    if os.getenv("LIVE_MCP_TOKEN_ENV"):
        params["token_env"] = os.environ["LIVE_MCP_TOKEN_ENV"]
    args = json.loads(os.getenv("LIVE_MCP_ARGS", "{}"))
    res = dispatch(build_tool("mcp", params), args, ToolContext(run_id="live", timeout_s=60))
    print("\nOK:", res.ok, "| META:", res.meta, "\nOUTPUT:\n", (res.output or "")[:800], "\nERROR:", res.error)
    assert res.ok, res.error
