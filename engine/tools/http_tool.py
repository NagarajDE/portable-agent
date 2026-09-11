"""
ENGINE / tools / http_tool -- the ONE generic, reusable HTTP GET reference adapter. Any
API-backed pack (a JIRA-style ticket reader, a REST metrics source, ...) reuses THIS with
config; we do not ship vendor-specific connectors. It is the reference implementation of
the network-safety contract:

  * ALLOWLIST      -- requests only to explicitly configured hosts (fail CLOSED: empty
                      allowlist denies everything).
  * SSRF GUARD     -- the target host must not resolve to a private / loopback / link-local /
                      reserved IP (blocks 169.254.169.254 metadata, 127.0.0.1, 10/8, ...).
  * REDIRECT CHECK -- redirects are followed MANUALLY (allow_redirects=False) and every hop
                      is re-validated against the allowlist + SSRF guard.
  * SECRETS        -- a bearer token is read ONLY from an env var (never from config/pack);
                      it is never logged (dispatch redacts) and never placed in the URL.
  * BOUNDED        -- response size cap, per-call timeout (ctx.timeout_s), bounded retries
                      with backoff on transient errors, optional min-interval rate limit.
  * UNTRUSTED      -- the body is returned as plain text evidence; it is never executed and
                      (by the deterministic loop) can never trigger a tool call.

Default mode is "mock" (creds-free canned responses) so packs/tests run with no network.
Set mode="http" + an allowlist to make real calls. `requests` is imported lazily.
"""
from __future__ import annotations

import ipaddress
import os
import socket
import threading
import time
from urllib.parse import urljoin, urlsplit

from pydantic import BaseModel

from engine.tools.base import ToolContext, ToolResult, ToolSpec, bound_output
from engine.tools.registry import register


class _HttpInput(BaseModel):
    path: str = ""                                   # appended to base_url
    query: dict = {}                                 # querystring params


# ---- pure, network-free safety helpers (unit-testable with IP/host literals) -------------
def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def is_host_allowed(host: str, allowlist) -> bool:
    """Exact match or dot-suffix match (api.example.com matches example.com). Empty -> deny."""
    if not host or not allowlist:
        return False
    host = host.lower()
    for allowed in allowlist:
        a = str(allowed).lower().strip()
        if a and (host == a or host.endswith("." + a)):
            return True
    return False


def resolves_to_blocked_ip(host: str) -> bool:
    """True if the host is, or DNS-resolves to, a non-public IP (SSRF guard). An IP literal is
    checked directly (no DNS); a name is resolved via getaddrinfo and ALL answers are checked."""
    def _blocked(ip_str: str) -> bool:
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return True                               # unparseable -> treat as blocked (fail closed)
        return (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified)
    try:
        ipaddress.ip_address(host)                    # host is already an IP literal
        return _blocked(host)
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return True                                   # cannot resolve -> block
    return any(_blocked(info[4][0]) for info in infos)


def validate_url(url: str, allowlist) -> str:
    """Raise ValueError unless the URL's host is allowlisted AND does not resolve to a
    blocked IP. Returns the URL on success."""
    if urlsplit(url).scheme not in ("http", "https"):
        raise ValueError(f"blocked non-http(s) URL: {url!r}")
    host = host_of(url)
    if not is_host_allowed(host, allowlist):
        raise ValueError(f"host {host!r} not in allowlist {sorted(allowlist)}")
    if resolves_to_blocked_ip(host):
        raise ValueError(f"host {host!r} resolves to a blocked (non-public) address")
    return url


def _env_allowlist() -> list[str]:
    raw = os.getenv("HTTP_TOOL_ALLOWLIST", "")
    return [h.strip() for h in raw.split(",") if h.strip()]


class HttpGetTool:
    def __init__(self, params: dict):
        self.spec = ToolSpec(
            name=params.get("tool_name", "http_get"),
            description=params.get("description", "HTTP GET over an allowlisted endpoint."),
            read_only=True,
            input_model=_HttpInput,
        )
        self._base = params.get("base_url", "")
        self._allow = list(params.get("allow_hosts") or _env_allowlist())
        self._mode = params.get("mode", os.getenv("HTTP_TOOL_MODE", "mock")).strip().lower()
        self._mock = params.get("mock_responses") or {}
        self._mock_default = params.get("mock_output", "")
        self._token_env = params.get("token_env")     # NAME of an env var; never the token itself
        self._max_bytes = max(1, int(params.get("max_bytes", 100_000)))
        self._retries = max(0, int(params.get("retries", 2)))
        self._backoff_s = float(params.get("backoff_s", 0.2) or 0)
        self._min_interval_s = float(params.get("min_interval_s", 0) or 0)
        self._max_redirects = max(0, int(params.get("max_redirects", 3)))
        self._last_call = 0.0
        self._lock = threading.Lock()

    def run(self, input: dict, ctx: ToolContext) -> ToolResult:
        url = urljoin(self._base + ("/" if self._base and not self._base.endswith("/") else ""),
                      input.get("path", "").lstrip("/")) if self._base else input.get("path", "")
        if self._mode == "mock":                       # creds-free deterministic path
            body = self._mock.get(input.get("path", ""), self._mock_default)
            return ToolResult(ok=True, output=bound_output(body, self._max_bytes),
                              meta={"mode": "mock", "path": input.get("path", "")})
        self._rate_limit()
        body = self._fetch(url, dict(input.get("query") or {}), ctx)
        return ToolResult(ok=True, output=bound_output(body, self._max_bytes),
                          meta={"mode": "http"})

    def _rate_limit(self) -> None:
        if self._min_interval_s <= 0:
            return
        with self._lock:
            wait = self._min_interval_s - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()

    def _headers(self) -> dict:
        headers = {"Accept": "application/json, text/plain", "User-Agent": "portable-agent"}
        if self._token_env:                            # secret from ENV only
            token = os.getenv(self._token_env)
            if token:
                headers["Authorization"] = f"Bearer {token}"
        return headers

    def _fetch(self, url: str, query: dict, ctx: ToolContext) -> str:
        import requests                                # lazy: only when a REAL call happens
        headers = self._headers()
        timeout = max(0.1, float(ctx.timeout_s))
        current, hops, last_exc = validate_url(url, self._allow), 0, None
        for attempt in range(self._retries + 1):
            try:
                while True:
                    resp = requests.get(current, headers=headers, params=query,
                                        timeout=timeout, allow_redirects=False, stream=True)
                    if resp.is_redirect or resp.status_code in (301, 302, 303, 307, 308):
                        loc = resp.headers.get("Location", "")
                        resp.close()
                        if hops >= self._max_redirects or not loc:
                            raise ValueError("too many redirects or missing Location")
                        current = validate_url(urljoin(current, loc), self._allow)  # re-validate each hop
                        query = {}                     # querystring already applied on hop 1
                        hops += 1
                        continue
                    resp.raise_for_status()
                    body = resp.raw.read(self._max_bytes + 1, decode_content=True)
                    resp.close()
                    text = body.decode("utf-8", errors="replace")
                    return text
            except (requests.ConnectionError, requests.Timeout) as e:  # transient -> retry w/ backoff
                last_exc = e
                if attempt < self._retries and self._backoff_s:
                    time.sleep(self._backoff_s * (2 ** attempt))
        raise ConnectionError(f"HTTP GET failed after {self._retries + 1} attempts: {last_exc}")


register("http", HttpGetTool)
