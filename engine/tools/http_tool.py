"""
ENGINE / tools / http_tool -- the ONE generic, reusable HTTP GET reference adapter. Any
API-backed pack (a JIRA-style ticket reader, a REST metrics source, ...) reuses THIS with
config; we do not ship vendor-specific connectors. It is the reference implementation of
the network-safety contract:

  * ALLOWLIST      -- requests only to explicitly configured hosts (fail CLOSED: an empty or
                      unset allowlist denies everything; an explicit `allow_hosts: []` also denies).
  * SSRF GUARD     -- the target host must not resolve to a private / loopback / link-local /
                      reserved / CGNAT (100.64/10) / unspecified IP; IPv4-mapped IPv6 is unwrapped;
                      an empty DNS answer fails closed. Blocks 169.254.169.254, 127.0.0.1, 10/8, ...
  * REDIRECT CHECK -- redirects are followed MANUALLY (allow_redirects=False) and every hop is
                      re-validated (host allowlist + SSRF) IMMEDIATELY before the connect, on every
                      attempt -- shrinking the DNS-rebinding window. (Residual: we do not pin the
                      resolved IP onto the socket; the allowlist is the primary control, since
                      rebinding still requires attacker DNS control of an already-allowlisted host.)
  * SECRETS        -- a bearer token is read ONLY from an env var (never config/pack). It is sent
                      ONLY to the ORIGINAL request origin AND only over https -- never forwarded to
                      a redirected origin and never over an http downgrade. Never logged (dispatch
                      redacts) and never placed in the URL.
  * NO AMBIENT     -- a private session with trust_env=False: no env proxies, no .netrc; URL
                      userinfo (user:pass@host) is rejected.
  * BOUNDED        -- response size cap, per-call timeout AND a wall-clock deadline across all
                      redirects/retries, bounded retries with backoff, optional rate limit; the
                      response is always closed.
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

from pydantic import BaseModel, ConfigDict

from engine.tools.base import ToolContext, ToolResult, ToolSpec, bound_output
from engine.tools.registry import register

# Shared address space (CGNAT, RFC 6598) -- not always flagged by ipaddress.is_private (M9).
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


class _HttpInput(BaseModel):
    model_config = ConfigDict(extra="forbid")        # reject unknown args instead of dropping them (M3)
    path: str = ""                                   # RELATIVE path appended to base_url
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


def _ip_is_blocked(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True                                   # unparseable -> treat as blocked (fail closed)
    mapped = getattr(ip, "ipv4_mapped", None)         # ::ffff:10.0.0.1 -> inspect the mapped v4
    if mapped is not None:
        ip = mapped
    if isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT:
        return True
    return (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def resolves_to_blocked_ip(host: str) -> bool:
    """True if the host is, or DNS-resolves to, a non-public IP (SSRF guard). An IP literal is
    checked directly (no DNS); a name is resolved via getaddrinfo and ALL answers are checked. An
    empty answer or a resolution failure fails CLOSED (blocked)."""
    try:
        ipaddress.ip_address(host)                    # host is already an IP literal
        return _ip_is_blocked(host)
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return True                                   # cannot resolve -> block
    if not infos:
        return True                                   # no addresses -> block (fail closed, M9)
    return any(_ip_is_blocked(info[4][0]) for info in infos)


def validate_url(url: str, allowlist) -> str:
    """Raise ValueError unless the URL is http(s), carries no userinfo, its host is allowlisted,
    and it does not resolve to a blocked IP. Returns the URL on success."""
    u = urlsplit(url)
    if u.scheme not in ("http", "https"):
        raise ValueError(f"blocked non-http(s) URL: {url!r}")
    if "@" in (u.netloc or ""):                       # ANY userinfo (incl. empty `@host`) rejected (L1)
        raise ValueError("URL must not contain userinfo")
    host = host_of(url)
    if not is_host_allowed(host, allowlist):
        raise ValueError(f"host {host!r} not in allowlist {sorted(allowlist)}")
    if resolves_to_blocked_ip(host):
        raise ValueError(f"host {host!r} resolves to a blocked (non-public) address")
    return url


def _origin(url: str) -> tuple:
    """(scheme, host, port) -- the identity a bearer token is bound to."""
    u = urlsplit(url)
    port = u.port or (443 if u.scheme == "https" else 80)
    return (u.scheme, (u.hostname or "").lower(), port)


def _env_allowlist() -> list[str]:
    raw = os.getenv("HTTP_TOOL_ALLOWLIST", "")
    return [h.strip() for h in raw.split(",") if h.strip()]


def _coerce_allow(value) -> list[str]:
    """Allowlist semantics (M10): unset (None) -> fall back to env; an explicit list -> use it
    (empty list = deny all, NO env fallback); a bare string -> a single host (not char-split)."""
    if value is None:
        return _env_allowlist()
    if isinstance(value, str):
        v = value.strip()
        return [v] if v else []
    return [str(h).strip() for h in value if str(h).strip()]


class HttpGetTool:
    def __init__(self, params: dict):
        self.spec = ToolSpec(
            name=params.get("tool_name", "http_get"),
            description=params.get("description", "HTTP GET over an allowlisted endpoint."),
            read_only=bool(params.get("read_only", True)),   # override for a known-mutating GET (M16)
            input_model=_HttpInput,
        )
        self._base = params.get("base_url", "")
        self._allow = _coerce_allow(params.get("allow_hosts"))
        self._mode = str(params.get("mode", os.getenv("HTTP_TOOL_MODE", "mock"))).strip().lower()
        if self._mode not in ("mock", "http"):        # anything else must NOT silently enable networking (M11)
            raise ValueError(f"http tool mode must be 'mock' or 'http', got {self._mode!r}")
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
        path = str(input.get("path", ""))
        pu = urlsplit(path)
        if pu.scheme or pu.netloc:                     # path must be RELATIVE (no scheme/host) (M12)
            raise ValueError("path must be relative (no scheme or host)")
        if ".." in pu.path.split("/"):                 # check the PATH COMPONENT only, so '..?x=1' can't slip by (M12)
            raise ValueError("path must not contain '..' segments")
        if self._mode == "mock":                       # creds-free deterministic path
            body = self._mock.get(path, self._mock_default)
            return ToolResult(ok=True, output=bound_output(body, self._max_bytes),
                              meta={"mode": "mock", "path": path})
        base = self._base + ("/" if self._base and not self._base.endswith("/") else "")
        url = urljoin(base, path.lstrip("/")) if self._base else path
        self._rate_limit()
        body = self._fetch(url, dict(input.get("query") or {}), ctx)
        return ToolResult(ok=True, output=bound_output(body, self._max_bytes), meta={"mode": "http"})

    def _rate_limit(self) -> None:
        if self._min_interval_s <= 0:
            return
        with self._lock:
            wait = self._min_interval_s - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()

    def _headers(self, token: str | None) -> dict:
        headers = {"Accept": "application/json, text/plain", "User-Agent": "portable-agent"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _read_body(self, resp, deadline, requests) -> bytes:
        """Read the body in bounded chunks, checking the wall-clock deadline between chunks so a
        slow-drip response can't run indefinitely past ctx.timeout_s (M14). Capped at max_bytes+1
        (the extra byte lets bound_output detect truncation). `raw` is accessed once (it's stateful)."""
        raw = resp.raw
        limit = self._max_bytes + 1
        buf = bytearray()
        while len(buf) < limit:
            if time.monotonic() > deadline:
                raise requests.Timeout("wall-clock deadline exceeded during body read")
            chunk = raw.read(min(65536, limit - len(buf)), decode_content=True)
            if not chunk:
                break
            buf += chunk
        return bytes(buf)

    def _fetch(self, url: str, query: dict, ctx: ToolContext) -> str:
        import requests                                # lazy: only when a REAL call happens
        import urllib3                                 # (a requests dependency) -> catch raw read errors
        origin = _origin(url)                         # computed BEFORE the session (N3: nothing to leak on failure)
        token = os.getenv(self._token_env) if self._token_env else None
        deadline = time.monotonic() + max(0.1, float(ctx.timeout_s))   # wall-clock bound (M14)
        retryable = (requests.ConnectionError, requests.Timeout, urllib3.exceptions.HTTPError)
        session = requests.Session()
        session.trust_env = False                     # ignore env proxies / .netrc / ambient creds (L1)
        last_exc = None
        try:
            for attempt in range(self._retries + 1):
                try:
                    current, hops = url, 0
                    params = dict(query)              # PER-ATTEMPT copy: a redirect clears the local copy,
                    while True:                       # never the original -> retries keep the query (N1)
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise requests.Timeout("wall-clock deadline exceeded")
                        validate_url(current, self._allow)             # re-validate right before connect (H5)
                        send_token = (token if (token and urlsplit(current).scheme == "https"
                                                and _origin(current) == origin) else None)
                        resp = session.get(current, headers=self._headers(send_token), params=params,
                                           timeout=max(0.1, remaining), allow_redirects=False, stream=True)
                        try:
                            if resp.is_redirect or resp.status_code in (301, 302, 303, 307, 308):
                                loc = resp.headers.get("Location", "")
                                if hops >= self._max_redirects or not loc:
                                    raise ValueError("too many redirects or missing Location")
                                current = urljoin(current, loc)        # re-validated at top of loop
                                params = {}                            # querystring already applied on hop 1
                                hops += 1
                                continue
                            resp.raise_for_status()
                            return self._read_body(resp, deadline, requests).decode("utf-8", errors="replace")
                        finally:
                            resp.close()                               # always close (M15)
                except retryable as e:                # transient (incl. raw urllib3 read errors) -> retry (N2)
                    last_exc = e
                    if attempt < self._retries:
                        nap = min(self._backoff_s * (2 ** attempt), max(0.0, deadline - time.monotonic()))
                        if nap > 0:
                            time.sleep(nap)
            raise ConnectionError(f"HTTP GET failed after {self._retries + 1} attempts: {last_exc}")
        finally:
            session.close()


register("http", HttpGetTool)
