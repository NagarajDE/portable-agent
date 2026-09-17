"""
ENGINE / deps -- ONE helper for the lazy, OPTIONAL vendor SDK imports (anthropic, snowflake-snowpark,
openai, litellm, databricks-sdk, mcp). Each adapter imports its SDK only when selected, so the mock path
needs none of them; when a selected SDK is absent the failure must be ACTIONABLE -- name the feature that
needs it and the pip package -- never a bare `ModuleNotFoundError: No module named 'databricks'`.
"""
from __future__ import annotations

import importlib


def require(module: str, *, package: str, feature: str):
    """Import `module` (e.g. 'databricks.sdk') or raise ImportError naming `feature` and `package`."""
    try:
        return importlib.import_module(module)
    except ImportError as e:
        raise ImportError(f"{feature} needs the `{package}` package, which is not installed in this "
                          f"environment (pip install {package}). Underlying error: {e}") from e
