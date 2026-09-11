"""
Network-safety tests for the ONE generic HTTP tool. No real network: the pure safety helpers
are tested with host/IP literals, and the real GET path is tested against a FAKE `requests`
module injected via sys.modules (so redirects, headers, and byte-bounds are deterministic).

Proves: allowlist (fail-closed), SSRF guard (private/loopback/link-local/reserved),
scheme check, per-hop redirect re-validation, bearer token sourced from ENV only, and the
response-size bound.
"""
import sys

import pytest

from engine.tools import build_tool, dispatch, ToolContext
from engine.tools import http_tool as H


def _ctx(timeout_s=2.0):
    return ToolContext(run_id="test", timeout_s=timeout_s, approved=False)


# --- pure allowlist / SSRF / scheme checks (no network) --------------------
def test_allowlist_exact_and_suffix():
    assert H.is_host_allowed("example.com", ["example.com"])
    assert H.is_host_allowed("api.example.com", ["example.com"])       # dot-suffix
    assert not H.is_host_allowed("notexample.com", ["example.com"])    # not a real suffix
    assert not H.is_host_allowed("evil.com", ["example.com"])


def test_allowlist_empty_denies_all():
    assert not H.is_host_allowed("example.com", [])                    # fail closed


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.5", "192.168.1.9", "169.254.169.254",
                                 "::1", "0.0.0.0", "224.0.0.1"])
def test_ssrf_blocks_non_public_ips(ip):
    assert H.resolves_to_blocked_ip(ip)


def test_ssrf_allows_public_ip_literal():
    assert not H.resolves_to_blocked_ip("8.8.8.8")


def test_validate_url_rejects_non_http_scheme():
    with pytest.raises(ValueError):
        H.validate_url("file:///etc/passwd", ["example.com"])
    with pytest.raises(ValueError):
        H.validate_url("ftp://example.com/x", ["example.com"])


def test_validate_url_rejects_disallowed_host(monkeypatch):
    monkeypatch.setattr(H, "resolves_to_blocked_ip", lambda h: False)  # bypass DNS
    with pytest.raises(ValueError):
        H.validate_url("https://evil.com/x", ["example.com"])


def test_validate_url_rejects_blocked_ip(monkeypatch):
    # host is allowlisted but resolves to a private address -> still blocked
    monkeypatch.setattr(H, "resolves_to_blocked_ip", lambda h: True)
    with pytest.raises(ValueError):
        H.validate_url("https://api.example.com/x", ["example.com"])


# --- mock mode: canned, deterministic, no network -------------------------
def test_http_mock_mode_returns_canned():
    tool = build_tool("http", {"mode": "mock",
                               "mock_responses": {"/status": "OK-STATUS"},
                               "mock_output": "DEFAULT"})
    assert dispatch(tool, {"path": "/status"}, _ctx()).output == "OK-STATUS"
    assert dispatch(tool, {"path": "/unknown"}, _ctx()).output == "DEFAULT"


# --- fake `requests` for the real GET path --------------------------------
class _Raw:
    """A STATEFUL raw stream: successive read()s advance a cursor (so a chunked reader terminates),
    and an optional exception is raised on the first read (to drive the body-error retry path)."""
    def __init__(self, body, read_exc=None):
        self._body, self._pos, self._read_exc = body, 0, read_exc

    def read(self, n, decode_content=True):
        if self._read_exc is not None:
            exc, self._read_exc = self._read_exc, None
            raise exc
        chunk = self._body[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk


class _Resp:
    def __init__(self, status=200, headers=None, body=b"", raise_for=None, read_exc=None):
        self.status_code, self.headers, self._raise = status, headers or {}, raise_for
        self.is_redirect = status in (301, 302, 303, 307, 308)
        self.raw = _Raw(body, read_exc)              # persistent + stateful

    def raise_for_status(self):
        if self._raise:
            raise self._raise

    def close(self):
        pass


class _FakeSession:
    """Models requests.Session: records (url, headers, params) per hop, replays scripted responses,
    and lets a scripted Exception be raised (to drive the retry path)."""
    def __init__(self, responses, calls):
        self._responses, self.calls, self.trust_env = responses, calls, True

    def get(self, url, headers=None, params=None, timeout=None, allow_redirects=None, stream=None):
        self.calls.append((url, dict(headers or {}), dict(params or {})))
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        pass


class _FakeRequests:
    ConnectionError = type("ConnectionError", (Exception,), {})
    Timeout = type("Timeout", (Exception,), {})

    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = []            # (url, headers, params) across all hops/sessions

    def Session(self):
        return _FakeSession(self._responses, self.calls)



@pytest.fixture
def fake_requests(monkeypatch):
    def _install(*responses):
        fake = _FakeRequests(*responses)
        monkeypatch.setitem(sys.modules, "requests", fake)
        monkeypatch.setattr(H, "resolves_to_blocked_ip", lambda h: False)  # bypass DNS in tests
        return fake
    return _install


def test_http_real_get_returns_body(fake_requests):
    fake_requests(_Resp(200, body=b'{"ok": true}'))
    tool = build_tool("http", {"mode": "http", "base_url": "https://api.example.com",
                               "allow_hosts": ["example.com"]})
    r = dispatch(tool, {"path": "/v1/thing"}, _ctx())
    assert r.ok and '{"ok": true}' in r.output


def test_http_bearer_token_comes_from_env_only(fake_requests, monkeypatch):
    fake = fake_requests(_Resp(200, body=b"data"))
    monkeypatch.setenv("MY_API_TOKEN", "SECRET-TOKEN-XYZ")
    tool = build_tool("http", {"mode": "http", "base_url": "https://api.example.com",
                               "allow_hosts": ["example.com"], "token_env": "MY_API_TOKEN"})
    dispatch(tool, {"path": "/x"}, _ctx())
    headers = fake.calls[0][1]
    assert headers.get("Authorization") == "Bearer SECRET-TOKEN-XYZ"    # value from env, not config


def test_http_no_token_env_means_no_auth_header(fake_requests):
    fake = fake_requests(_Resp(200, body=b"data"))
    tool = build_tool("http", {"mode": "http", "base_url": "https://api.example.com",
                               "allow_hosts": ["example.com"]})
    dispatch(tool, {"path": "/x"}, _ctx())
    headers = fake.calls[0][1]
    assert "Authorization" not in headers


def test_http_redirect_to_disallowed_host_is_blocked(fake_requests):
    # first hop 302 -> Location on a host NOT in the allowlist: must be rejected on re-validation
    fake_requests(_Resp(302, headers={"Location": "https://evil.com/steal"}))
    tool = build_tool("http", {"mode": "http", "base_url": "https://api.example.com",
                               "allow_hosts": ["example.com"], "max_redirects": 3})
    r = dispatch(tool, {"path": "/x"}, _ctx())
    assert not r.ok and "allowlist" in r.error.message   # rejected for the RIGHT reason (off-allowlist)


def test_http_redirect_within_allowlist_is_followed(fake_requests):
    fake = fake_requests(
        _Resp(302, headers={"Location": "https://api.example.com/moved"}),
        _Resp(200, body=b"FINAL"),
    )
    tool = build_tool("http", {"mode": "http", "base_url": "https://api.example.com",
                               "allow_hosts": ["example.com"], "max_redirects": 3})
    r = dispatch(tool, {"path": "/x"}, _ctx())
    assert r.ok and "FINAL" in r.output and len(fake.calls) == 2


def test_http_response_size_is_bounded(fake_requests):
    fake_requests(_Resp(200, body=b"A" * 10_000))
    tool = build_tool("http", {"mode": "http", "base_url": "https://api.example.com",
                               "allow_hosts": ["example.com"], "max_bytes": 500})
    r = dispatch(tool, {"path": "/big"}, _ctx())
    assert r.ok and len(r.output) <= 500 + 80          # bounded to max_bytes (+ short truncation note)


# --- H6: bearer token is bound to the original origin + https only ------------------------
def test_http_token_dropped_on_redirect_to_other_origin(fake_requests, monkeypatch):
    fake = fake_requests(
        _Resp(302, headers={"Location": "https://other.example.com/moved"}),
        _Resp(200, body=b"OK"),
    )
    monkeypatch.setenv("TOK", "SECRET-XYZ")
    tool = build_tool("http", {"mode": "http", "base_url": "https://api.example.com",
                               "allow_hosts": ["example.com"], "token_env": "TOK", "max_redirects": 3})
    assert dispatch(tool, {"path": "/x"}, _ctx()).ok
    assert fake.calls[0][1].get("Authorization") == "Bearer SECRET-XYZ"   # hop 1: same origin -> sent
    assert "Authorization" not in fake.calls[1][1]                        # hop 2: other origin -> dropped


def test_http_token_not_sent_over_http_downgrade(fake_requests, monkeypatch):
    fake = fake_requests(_Resp(200, body=b"OK"))
    monkeypatch.setenv("TOK", "SECRET-XYZ")
    tool = build_tool("http", {"mode": "http", "base_url": "http://api.example.com",   # http, not https
                               "allow_hosts": ["example.com"], "token_env": "TOK"})
    assert dispatch(tool, {"path": "/x"}, _ctx()).ok
    assert "Authorization" not in fake.calls[0][1]                        # never send a token over http


# --- M9: SSRF classification completeness -------------------------------------------------
def test_ssrf_blocks_cgnat_and_ipv4_mapped():
    assert H.resolves_to_blocked_ip("100.64.0.1")       # carrier-grade NAT (RFC 6598)
    assert H.resolves_to_blocked_ip("::ffff:127.0.0.1")  # IPv4-mapped loopback


# --- M10: allowlist semantics -------------------------------------------------------------
def test_allow_hosts_explicit_empty_denies(fake_requests, monkeypatch):
    monkeypatch.setenv("HTTP_TOOL_ALLOWLIST", "example.com")   # env has a host...
    tool = build_tool("http", {"mode": "http", "base_url": "https://api.example.com",
                               "allow_hosts": []})              # ...but explicit [] must NOT fall back
    r = dispatch(tool, {"path": "/x"}, _ctx())
    assert not r.ok and "allowlist" in r.error.message        # host denied (no env fallback)


def test_allow_hosts_string_is_single_host(fake_requests):
    fake_requests(_Resp(200, body=b"OK"))
    tool = build_tool("http", {"mode": "http", "base_url": "https://api.example.com",
                               "allow_hosts": "example.com"})   # a bare string, not char-split
    assert dispatch(tool, {"path": "/x"}, _ctx()).ok


# --- M11: mode validation -----------------------------------------------------------------
def test_bad_mode_raises_at_construction():
    with pytest.raises(ValueError):
        build_tool("http", {"mode": "offline"})                # a typo must NOT silently enable networking


# --- M12: path must be relative, no parent-dir escape -------------------------------------
def test_path_absolute_url_rejected(fake_requests):
    fake_requests(_Resp(200, body=b"x"))
    tool = build_tool("http", {"mode": "http", "base_url": "https://api.example.com",
                               "allow_hosts": ["example.com"]})
    r = dispatch(tool, {"path": "http://evil.com/"}, _ctx())
    assert not r.ok and "relative" in r.error.message


def test_path_dotdot_rejected(fake_requests):
    fake_requests(_Resp(200, body=b"x"))
    tool = build_tool("http", {"mode": "http", "base_url": "https://api.example.com",
                               "allow_hosts": ["example.com"]})
    r = dispatch(tool, {"path": "../../admin"}, _ctx())
    assert not r.ok and "segment" in r.error.message


def test_path_dotdot_with_query_rejected(fake_requests):
    # M12: `..?x=1` has no '/' delimiter, but the PATH COMPONENT is '..' -> must still be rejected
    fake_requests(_Resp(200, body=b"x"))               # queued 200: if the check regressed this would pass ok
    tool = build_tool("http", {"mode": "http", "base_url": "https://api.example.com",
                               "allow_hosts": ["example.com"]})
    r = dispatch(tool, {"path": "..?x=1"}, _ctx())
    assert not r.ok and "segment" in r.error.message


def test_path_encoded_dotdot_rejected(fake_requests):
    # NB2: percent-encoded traversal (%2e%2e) must be decoded and rejected before it reaches the wire
    fake_requests(_Resp(200, body=b"x"))
    tool = build_tool("http", {"mode": "http", "base_url": "https://api.example.com",
                               "allow_hosts": ["example.com"]})
    r = dispatch(tool, {"path": "%2e%2e/admin"}, _ctx())
    assert not r.ok and "segment" in r.error.message


def test_validate_url_rejects_any_userinfo():
    # L1: empty userinfo (`@host`) is falsy but must still be rejected
    for u in ("https://user:pw@api.example.com/x", "https://@api.example.com/x",
              "https://user@api.example.com/x"):
        with pytest.raises(ValueError):
            H.validate_url(u, ["example.com"])


def test_http_retry_after_redirect_keeps_query(fake_requests):
    # N1: a redirect clears the querystring for the redirected hop; a subsequent retry must restart
    # from the ORIGINAL url WITH the original query (not the cleared one).
    fake = fake_requests(
        _Resp(302, headers={"Location": "https://api.example.com/moved"}),  # attempt1 hop0 -> redirect
        _FakeRequests.ConnectionError("dropped"),                            # attempt1 hop1 -> transient
        _Resp(200, body=b"OK"),                                              # attempt2 hop0 -> success
    )
    tool = build_tool("http", {"mode": "http", "base_url": "https://api.example.com",
                               "allow_hosts": ["example.com"], "retries": 1, "backoff_s": 0})
    r = dispatch(tool, {"path": "/x", "query": {"tenant": "A"}}, _ctx())
    assert r.ok and "OK" in r.output
    assert fake.calls[-1][2] == {"tenant": "A"}         # final (successful) request kept the query


def test_http_body_read_error_is_retried(fake_requests):
    # N2: a raw urllib3 error during body read must be retried, not terminate immediately
    import urllib3
    fake = fake_requests(
        _Resp(200, body=b"", read_exc=urllib3.exceptions.ProtocolError("boom")),  # attempt1 read fails
        _Resp(200, body=b"RECOVERED"),                                            # attempt2 ok
    )
    tool = build_tool("http", {"mode": "http", "base_url": "https://api.example.com",
                               "allow_hosts": ["example.com"], "retries": 1, "backoff_s": 0})
    r = dispatch(tool, {"path": "/x"}, _ctx())
    assert r.ok and "RECOVERED" in r.output and len(fake.calls) == 2
