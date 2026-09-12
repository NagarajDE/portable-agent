"""Test isolation: every test starts with the app's env vars unset (monkeypatch restores
them afterward), so no test leaks configuration into another (N5)."""
import pytest

_APP_ENV = [
    "USE_CASE", "WORKER_PROVIDER", "WORKER_MODEL", "EVAL_PROVIDER", "EVAL_MODEL",
    "SQL_TOOL", "SQL_MAX_ROWS", "SQL_TIMEOUT_SECONDS",
    "MEMORY_STORE", "MEMORY_SQLITE_PATH",
    "TRACER", "TRACE_INCLUDE_CONTENT", "LLM_MAX_TOKENS",
    "TOOL_TIMEOUT_SECONDS", "TOOL_MAX_CONCURRENCY", "HTTP_TOOL_MODE", "HTTP_TOOL_ALLOWLIST",
    "LITELLM_MODEL", "LITELLM_BASE_URL",
]


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    for var in _APP_ENV:
        monkeypatch.delenv(var, raising=False)
    import engine.memory as memory
    memory._instance = None                 # don't inherit a store a prior (e.g. shell) test built
    yield
    memory._instance = None                 # and don't leak ours to the next test
