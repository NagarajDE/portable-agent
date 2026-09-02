"""
ENGINE / provider-agnostic inference.  GENERIC -- no domain knowledge lives here.

The loop imports get_llm_client() and calls .complete().  It never imports
anthropic / snowflake / databricks.  Switch provider with one env var:
    LLM_PROVIDER=mock|anthropic|cortex|databricks

Note: the MOCK's *mechanic* (climbing score, revision stamping) is generic and
lives here; the actual answer TEXT it fakes comes from the active use-case's
fixtures.py -- so the engine stays domain-free.
"""
from __future__ import annotations
import os
import re
import importlib
from typing import Protocol, runtime_checkable


@runtime_checkable
class LLMClient(Protocol):
    def complete(self, prompt: str, **kw) -> str: ...


# --------------------------------------------------------------------------
# MOCK -- deterministic, zero credentials. Fakes an LLM that drafts, then
# improves each refine round, and a judge whose score climbs with revision.
# Domain text is loaded from the use-case; the loop logic here is generic.
# --------------------------------------------------------------------------
class MockClient:
    def __init__(self, use_case: str | None = None):
        self.canned = _load_canned(use_case)

    def complete(self, prompt: str, **kw) -> str:
        if "SCORE THE ANSWER" in prompt:                     # judge call
            rev = _answer_rev(prompt)
            return f"SCORE: {min(12 + 2 * rev, 18)}/18 - mock judge on rev {rev}"
        n = _next_rev(prompt)                                # generate / refine call
        text = self.canned["refined"] if "IMPROVE" in prompt else self.canned["draft"]
        return f"{text} [rev {n}]"


def _load_canned(use_case: str | None) -> dict:
    fallback = {"draft": "A first-pass answer based on the data.",
                "refined": "A specific, corrected answer based on the data."}
    if not use_case:
        return fallback
    try:
        mod = importlib.import_module(f"usecases.{use_case}.fixtures")
        return getattr(mod, "CANNED", fallback)
    except ModuleNotFoundError:
        return fallback


def _answer_rev(text: str) -> int:      # reads "[rev N]" inside the answer
    m = re.search(r"rev (\d+)", text)
    return int(m.group(1)) if m else 0


def _next_rev(text: str) -> int:        # reads "NEXT_REVISION=N" from the instruction
    m = re.search(r"NEXT_REVISION=(\d+)", text)
    return int(m.group(1)) if m else 0


# --------------------------------------------------------------------------
# REAL ADAPTERS -- same .complete() signature. SDKs imported lazily so the
# mock path never needs them installed. Fill the TODO, set the env var, done.
# --------------------------------------------------------------------------
class AnthropicClient:
    def __init__(self):
        import anthropic
        self._c = anthropic.Anthropic()                 # reads ANTHROPIC_API_KEY
        self._model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")

    def complete(self, prompt: str, **kw) -> str:
        r = self._c.messages.create(
            model=self._model, max_tokens=1024,
            messages=[{"role": "user", "content": prompt}])
        return r.content[0].text


class CortexClient:
    """Snowflake Cortex COMPLETE via Snowpark session."""
    def __init__(self):
        from snowflake.snowpark import Session
        self._s = Session.builder.configs(sf_cfg()).create()
        self._model = os.getenv("CORTEX_MODEL", "claude-3-5-sonnet")

    def complete(self, prompt: str, **kw) -> str:
        row = self._s.sql("SELECT SNOWFLAKE.CORTEX.COMPLETE(?, ?) AS R",
                          params=[self._model, prompt]).collect()[0]
        return row["R"]


class DatabricksClient:
    """Databricks Foundation Model API (OpenAI-compatible serving endpoint)."""
    def __init__(self):
        from openai import OpenAI
        self._c = OpenAI(api_key=os.environ["DATABRICKS_TOKEN"],
                         base_url=f"{os.environ['DATABRICKS_HOST']}/serving-endpoints")
        self._model = os.getenv("DATABRICKS_MODEL", "databricks-claude-sonnet-4")

    def complete(self, prompt: str, **kw) -> str:
        r = self._c.chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": prompt}])
        return r.choices[0].message.content


def sf_cfg() -> dict:
    return {k: os.environ[f"SNOWFLAKE_{k.upper()}"]
            for k in ("account", "user", "password", "warehouse", "role")}


# The only place a provider name is mentioned.
def get_llm_client(use_case: str | None = None) -> LLMClient:
    provider = os.getenv("LLM_PROVIDER", "mock").lower()
    if provider == "mock":
        return MockClient(use_case)
    return {"anthropic": AnthropicClient,
            "cortex": CortexClient,
            "databricks": DatabricksClient}[provider]()
