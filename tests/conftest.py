"""Test isolation: every test starts with the app's env vars unset (monkeypatch restores
them afterward), so no test leaks configuration into another (N5)."""
import pytest

# Every env var the engine/runners actually read (enumerated from os.getenv / os.environ across
# engine/ + runner_env.py). Kept complete so a developer's exported shell vars -- creds, a semantic
# view, a provider override -- can never leak into a test and silently change its behavior (or trigger
# a live call). Grouped by concern; add here when a new os.getenv is introduced.
_APP_ENV = [
    # loop / provider selection
    "USE_CASE", "WORKER_PROVIDER", "WORKER_MODEL", "EVAL_PROVIDER", "EVAL_MODEL",
    "LLM_MAX_TOKENS", "LITELLM_MODEL", "LITELLM_BASE_URL",
    "ANTHROPIC_MODEL", "CORTEX_MODEL", "DATABRICKS_MODEL",   # per-provider default model
    # SQL tool + semantic-layer override (runner_env)
    "SQL_TOOL", "SQL_MAX_ROWS", "SQL_TIMEOUT_SECONDS",
    "CORTEX_SEMANTIC_VIEW", "CORTEX_SEMANTIC_MODEL", "CORTEX_ANALYST_TIMEOUT_SECONDS",
    # Snowflake / Databricks connection + secrets (never let a shell creds env reach a test)
    "SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_PAT", "SNOWFLAKE_HOST",
    "SNOWFLAKE_SECONDARY_ROLES", "SNOWFLAKE_WAREHOUSE", "SNOWFLAKE_ROLE",
    "SNOWFLAKE_DATABASE", "SNOWFLAKE_SCHEMA",
    "DATABRICKS_HOST", "DATABRICKS_TOKEN",
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY",                   # SDK-read secrets: prevent stray live calls
    # memory / tracing / generic tool layer
    "MEMORY_STORE", "MEMORY_SQLITE_PATH",
    "TRACER", "TRACE_INCLUDE_CONTENT", "OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_SERVICE_NAME",
    "TOOL_TIMEOUT_SECONDS", "TOOL_MAX_CONCURRENCY", "HTTP_TOOL_MODE", "HTTP_TOOL_ALLOWLIST",
]


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    for var in _APP_ENV:
        monkeypatch.delenv(var, raising=False)
    import engine.memory as memory
    memory._instance = None                 # don't inherit a store a prior (e.g. shell) test built
    yield
    memory._instance = None                 # and don't leak ours to the next test
