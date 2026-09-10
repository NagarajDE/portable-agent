"""
ENGINE / provider-agnostic inference.  GENERIC -- no domain knowledge lives here.

The loop imports get_llm_client() (worker) and get_eval_client() (judge) and
calls .complete().  It never imports anthropic / snowflake / databricks.

Worker and evaluator are selected INDEPENDENTLY, each by a provider + model.
'auto' (or unset) means "use the sensible default":
    worker :  WORKER_PROVIDER=mock|anthropic|cortex|databricks   WORKER_MODEL=<name|auto>
    judge  :  EVAL_PROVIDER=<provider|auto>                  EVAL_MODEL=<name|auto>
    auto ->  worker provider = mock; evaluator provider = same as worker;
             either model = that provider's own default.
So you can run a different model under the SAME provider, or different providers
entirely, for worker vs. judge. Judging with a different model breaks the
self-grading blind spot (a model shouldn't score its own output).
Model precedence per role:  WORKER_MODEL / EVAL_MODEL  >  <PROVIDER>_MODEL  >  built-in default.

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


class EmptyResponseError(RuntimeError):
    """A provider returned an empty / whitespace-only completion. A distinct type so the
    evaluator retry loop can retry THIS (a bad response shape) without also swallowing
    auth/config errors, which stay as their own exception types and propagate."""


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
            m = re.search(r"SCORE:\s*N\s*/\s*(\d+)", prompt)  # honor the rubric's declared scale
            cap = int(m.group(1)) if m else 18
            return f"SCORE: {min(12 + 2 * rev, cap)}/{cap} - mock judge on rev {rev}"
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
def _max_tokens() -> int:
    """Output token cap. Default 4096 -- ample for our short answers/one-line verdicts, so
    truncation is unlikely; raise LLM_MAX_TOKENS if you generate longer outputs."""
    return int(os.getenv("LLM_MAX_TOKENS", "4096"))


class AnthropicClient:
    def __init__(self, model: str | None = None):
        import anthropic
        self._c = anthropic.Anthropic(timeout=60.0, max_retries=2)   # reads ANTHROPIC_API_KEY
        self._model = model or os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")

    def complete(self, prompt: str, **kw) -> str:
        r = self._c.messages.create(
            model=self._model, max_tokens=_max_tokens(),
            messages=[{"role": "user", "content": prompt}])
        if r.stop_reason == "max_tokens":               # truncated: don't treat a cut-off reply as complete
            raise RuntimeError("Anthropic output truncated (hit max_tokens); raise LLM_MAX_TOKENS")
        text = "\n".join(b.text for b in r.content if b.type == "text").strip()
        if not text:                                    # don't return an empty/None answer
            raise EmptyResponseError("Anthropic returned no text content")
        return text


class CortexClient:
    """Snowflake Cortex COMPLETE via a Snowpark session (SPCS OAuth token or local creds)."""
    def __init__(self, model: str | None = None):
        self._s = snowpark_session()
        self._model = model or os.getenv("CORTEX_MODEL", "claude-3-5-sonnet")

    def complete(self, prompt: str, **kw) -> str:
        row = self._s.sql("SELECT SNOWFLAKE.CORTEX.COMPLETE(?, ?) AS R",
                          params=[self._model, prompt]).collect()[0]
        text = row["R"]
        if not text or not str(text).strip():
            raise EmptyResponseError("Cortex COMPLETE returned no text")
        return text


class DatabricksClient:
    """Databricks Foundation Model API (OpenAI-compatible serving endpoint)."""
    def __init__(self, model: str | None = None):
        from urllib.parse import urlsplit
        from openai import OpenAI
        token = os.getenv("DATABRICKS_TOKEN", "").strip()
        host = os.getenv("DATABRICKS_HOST", "").strip()
        if not token:
            raise RuntimeError("DATABRICKS_TOKEN is required")
        u = urlsplit(host)                              # reject creds-in-URL, paths, query, non-https
        if u.scheme != "https" or not u.hostname or u.username or u.password \
                or u.path not in ("", "/") or u.query or u.fragment:
            raise ValueError("DATABRICKS_HOST must be an HTTPS workspace origin")
        self._c = OpenAI(api_key=token, base_url=f"{host.rstrip('/')}/serving-endpoints",
                         timeout=60.0, max_retries=2)
        self._model = model or os.getenv("DATABRICKS_MODEL", "databricks-claude-sonnet-4")

    def complete(self, prompt: str, **kw) -> str:
        r = self._c.chat.completions.create(
            model=self._model, max_tokens=_max_tokens(),
            messages=[{"role": "user", "content": prompt}])
        choice = r.choices[0] if r.choices else None
        if choice and choice.finish_reason == "length":  # truncated: don't treat as complete
            raise RuntimeError("Databricks output truncated (finish_reason=length); raise LLM_MAX_TOKENS")
        text = choice.message.content if choice else None
        if not text or not text.strip():
            raise EmptyResponseError("Databricks endpoint returned no text content")
        return text


def sf_cfg() -> dict:
    """Local Snowpark connection config. Auth uses ONE secret -- the same SNOWFLAKE_PAT the
    Cortex Analyst REST call uses -- passed in place of a password (Snowflake accepts a
    Programmatic Access Token anywhere a password is accepted). There is deliberately no
    separate SNOWFLAKE_PASSWORD. Account + user are required; warehouse/role/database/schema
    are added when set, else the user's own defaults apply."""
    # Pass ACCOUNT only (not host) and let the connector resolve the real endpoint -- forcing an
    # explicit host can send the auth request to the wrong deployment and get a valid PAT rejected.
    # (SNOWFLAKE_HOST is still used, separately, by the Cortex Analyst REST call.)
    cfg = {"account": os.environ["SNOWFLAKE_ACCOUNT"],
           "user": os.environ["SNOWFLAKE_USER"],
           "password": os.environ["SNOWFLAKE_PAT"]}       # PAT used as the password (single secret)
    for env, key in (("SNOWFLAKE_WAREHOUSE", "warehouse"), ("SNOWFLAKE_ROLE", "role"),
                     ("SNOWFLAKE_DATABASE", "database"), ("SNOWFLAKE_SCHEMA", "schema")):
        if os.getenv(env):
            cfg[key] = os.environ[env]
    return cfg


# --- Snowflake auth shared by CortexClient (COMPLETE) and CortexAnalystTool (Analyst REST).
#     Inside SPCS: use the OAuth token Snowflake injects at /snowflake/session/token.
#     Locally:     ONE Programmatic Access Token (SNOWFLAKE_PAT) authenticates BOTH the Snowpark
#                  session (as the password) and the Analyst REST call (as a bearer token).
_SPCS_TOKEN = "/snowflake/session/token"


def _apply_secondary_roles(session):
    """Optionally activate secondary roles so the session uses the UNION of privileges from ALL
    the user's granted roles -- needed when the primary SNOWFLAKE_ROLE lacks a grant some other
    role has (e.g. SNOWFLAKE.CORTEX_USER, which otherwise makes SNOWFLAKE.CORTEX.COMPLETE look
    like an 'unknown function'). Opt-in via SNOWFLAKE_SECONDARY_ROLES=all|none (unset = leave as-is;
    wraps Snowpark's use_secondary_roles / the USE SECONDARY ROLES SQL command)."""
    val = (os.getenv("SNOWFLAKE_SECONDARY_ROLES") or "").strip().lower()
    if val in ("all", "none"):
        session.use_secondary_roles(val)
    elif val:
        raise ValueError("SNOWFLAKE_SECONDARY_ROLES must be 'all', 'none', or unset")
    return session


def snowpark_session():
    """A Snowpark Session that works both inside SPCS (injected OAuth token) and locally
    (a single SNOWFLAKE_PAT via sf_cfg -- no password). Honors SNOWFLAKE_SECONDARY_ROLES."""
    from snowflake.snowpark import Session
    if os.path.exists(_SPCS_TOKEN):
        with open(_SPCS_TOKEN) as f:
            token = f.read()
        cfg = {"host": os.environ["SNOWFLAKE_HOST"],
               "account": os.environ["SNOWFLAKE_ACCOUNT"],
               "token": token, "authenticator": "oauth"}
        for env, key in (("SNOWFLAKE_WAREHOUSE", "warehouse"), ("SNOWFLAKE_DATABASE", "database"),
                         ("SNOWFLAKE_SCHEMA", "schema"), ("SNOWFLAKE_ROLE", "role")):
            if os.getenv(env):
                cfg[key] = os.environ[env]
        return _apply_secondary_roles(Session.builder.configs(cfg).create())
    return _apply_secondary_roles(Session.builder.configs(sf_cfg()).create())   # local: single PAT


def snowflake_rest_base() -> str:
    """Base URL for Snowflake REST APIs (e.g. Cortex Analyst)."""
    host = os.getenv("SNOWFLAKE_HOST")
    if not host:
        raise RuntimeError("SNOWFLAKE_HOST is required for Cortex Analyst REST calls.")
    return f"https://{host}"


def snowflake_bearer_headers() -> dict:
    """Bearer auth headers for Snowflake REST. SPCS OAuth token if present, else a
    Programmatic Access Token from SNOWFLAKE_PAT (handy for local testing)."""
    if os.path.exists(_SPCS_TOKEN):
        with open(_SPCS_TOKEN) as f:
            token, ttype = f.read(), "OAUTH"
    elif os.getenv("SNOWFLAKE_PAT"):
        token, ttype = os.environ["SNOWFLAKE_PAT"], "PROGRAMMATIC_ACCESS_TOKEN"
    else:
        raise RuntimeError("No Snowflake token: run inside SPCS, or set SNOWFLAKE_PAT "
                           "(+ SNOWFLAKE_HOST) for local Cortex Analyst REST calls.")
    return {"Authorization": f"Bearer {token}",
            "X-Snowflake-Authorization-Token-Type": ttype,
            "Content-Type": "application/json", "Accept": "application/json"}


# The only place provider names are mentioned.
_PROVIDERS = {"anthropic": AnthropicClient,
              "cortex": CortexClient,
              "databricks": DatabricksClient}


def _build(provider: str, use_case: str | None, model: str | None) -> LLMClient:
    provider = provider.lower()
    if provider == "mock":
        return MockClient(use_case)
    return _PROVIDERS[provider](model)


def _resolve(val: str | None, default):
    """Unset or 'auto' -> use the default. For a model, default=None means
    'let the provider's client pick its own default model'. Trims whitespace so a stray
    ' cortex ' doesn't become an unknown-provider KeyError."""
    if val is None or val.strip().lower() in ("", "auto"):
        return default
    return val.strip()


def get_llm_client(use_case: str | None = None) -> LLMClient:
    """The WORKER: generates and refines. Selected by provider + model:
        WORKER_PROVIDER   worker provider   (auto -> mock)
        WORKER_MODEL      worker model      (auto -> that provider's default)
    """
    provider = _resolve(os.getenv("WORKER_PROVIDER"), "mock")
    return _build(provider, use_case, _resolve(os.getenv("WORKER_MODEL"), None))


def get_eval_client(use_case: str | None = None) -> LLMClient:
    """The EVALUATOR: scores against the rubric. Judge independence is CONFIGURABLE, not
    automatic. Defaults: EVAL_PROVIDER 'auto' -> the WORKER's provider; EVAL_MODEL 'auto' ->
    that provider's default model (NOT necessarily a pinned WORKER_MODEL). So with everything
    'auto' the judge shares the worker's provider (and its model too, when WORKER_MODEL is
    also 'auto') -- i.e. the worker grades its own output. Set EVAL_PROVIDER / EVAL_MODEL to
    a different model for a genuinely independent judge:
        EVAL_PROVIDER   evaluator provider   (auto -> same as WORKER_PROVIDER)
        EVAL_MODEL      evaluator model      (auto -> that provider's default)
    Lets you judge with a different model on the SAME provider, or a different
    provider entirely.
    """
    worker_provider = _resolve(os.getenv("WORKER_PROVIDER"), "mock")
    provider = _resolve(os.getenv("EVAL_PROVIDER"), worker_provider)
    return _build(provider, use_case, _resolve(os.getenv("EVAL_MODEL"), None))


def model_summary() -> str:
    """One-line resolved worker/evaluator selection, for the build log."""
    wp = _resolve(os.getenv("WORKER_PROVIDER"), "mock")
    ep = _resolve(os.getenv("EVAL_PROVIDER"), wp)
    wm = _resolve(os.getenv("WORKER_MODEL"), "auto")
    em = _resolve(os.getenv("EVAL_MODEL"), "auto")
    return f"worker={wp}:{wm}  evaluator={ep}:{em}"
