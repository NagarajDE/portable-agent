"""AI gateway = LiteLLM adopted as a dependency. Tests: the LiteLLMClient adapter (with litellm
stubbed -- the SDK is imported lazily, so it need not be installed), and provider/model precedence
env > pack `models:` > default. Offline/deterministic."""
import sys
import types

import pytest

import engine.llm_client as L


def _stub_litellm(monkeypatch, *, content="hello", finish_reason="stop", capture=None):
    """Install a fake `litellm` module so LiteLLMClient.complete() runs without the real SDK."""
    mod = types.ModuleType("litellm")

    def completion(**kwargs):
        if capture is not None:
            capture.update(kwargs)
        msg = types.SimpleNamespace(content=content)
        choice = types.SimpleNamespace(message=msg, finish_reason=finish_reason)
        return types.SimpleNamespace(choices=[choice])

    mod.completion = completion
    monkeypatch.setitem(sys.modules, "litellm", mod)
    return mod


def test_litellm_client_completes_via_model_string(monkeypatch):
    cap = {}
    _stub_litellm(monkeypatch, content="the answer", capture=cap)
    c = L.LiteLLMClient(model="anthropic/claude-sonnet-4-5")
    assert c.complete("hi") == "the answer"
    assert cap["model"] == "anthropic/claude-sonnet-4-5"          # model string is the routing key


def test_litellm_needs_a_model():
    with pytest.raises(ValueError):
        L.LiteLLMClient()                                         # no model, no LITELLM_MODEL env


def test_litellm_truncation_raises(monkeypatch):
    _stub_litellm(monkeypatch, content="cut", finish_reason="length")
    with pytest.raises(RuntimeError):
        L.LiteLLMClient(model="openai/gpt-4o").complete("hi")     # finish_reason=length -> truncated


def test_litellm_empty_raises(monkeypatch):
    _stub_litellm(monkeypatch, content="   ")
    with pytest.raises(L.EmptyResponseError):
        L.LiteLLMClient(model="openai/gpt-4o").complete("hi")


def test_litellm_base_url_from_env(monkeypatch):
    cap = {}
    _stub_litellm(monkeypatch, capture=cap)
    monkeypatch.setenv("LITELLM_BASE_URL", "https://litellm.internal:4000")
    L.LiteLLMClient(model="openai/gpt-4o").complete("hi")
    assert cap["api_base"] == "https://litellm.internal:4000"     # -> a proxy / Databricks AI Gateway


def test_provider_precedence_env_over_pack_over_default(monkeypatch):
    cfg_models = {"worker": {"provider": "litellm", "model": "anthropic/claude-sonnet-4-5"}}
    # pack default (no env) -> uses pack profile
    monkeypatch.setenv("LITELLM_MODEL", "x/y")   # so LiteLLMClient construction doesn't need WORKER_MODEL
    c = L.get_llm_client("dq_qals", cfg_models)
    assert isinstance(c, L.LiteLLMClient)
    # env WINS over the pack profile
    monkeypatch.setenv("WORKER_PROVIDER", "mock")
    c2 = L.get_llm_client("dq_qals", cfg_models)
    assert isinstance(c2, L.MockClient)


def test_no_config_defaults_to_mock():
    assert isinstance(L.get_llm_client("dq_qals"), L.MockClient)
