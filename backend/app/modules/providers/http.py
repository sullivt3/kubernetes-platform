"""Small HTTPS client shared by API-based providers (Proxmox, AWS, Azure, GCP).

Why not vendor SDKs: boto3/azure-sdk/google-cloud each pull large dependency trees
(and some pin libraries Celery also pins). Providers only need a handful of REST
calls, and owning the transport gives one place for timeouts, TLS policy, error
mapping and a test seam (:func:`set_transport`).

TLS is always verified. A provider may supply a custom CA bundle (PEM) or pin the
server certificate's SHA-256 fingerprint, which is how self-signed Proxmox
installations are reached securely.
"""

from __future__ import annotations

import json
import ssl
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import urllib3

from app.core.errors import PlatformError

CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 60.0


class ProviderApiError(PlatformError):
    """A provider API call failed. ``status`` is the HTTP status (0 when unreachable)."""

    status_code = 502
    code = "provider_error"

    def __init__(self, message: str, *, status: int = 0, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, details={"status": status, **(details or {})})
        self.status = status


class ProviderUnreachableError(ProviderApiError):
    code = "provider_unreachable"


class ProviderAuthError(ProviderApiError):
    code = "provider_auth_failed"


@dataclass
class HttpRequest:
    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes | None = None


@dataclass
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body or b"null")

    @property
    def text(self) -> str:
        return self.body.decode(errors="replace")


@dataclass(frozen=True)
class TlsPolicy:
    ca_pem: str | None = None
    fingerprint_sha256: str | None = None  # "AA:BB:..." or plain hex


Transport = Callable[[HttpRequest, TlsPolicy], HttpResponse]


def _normalize_fingerprint(value: str) -> str:
    return value.replace(":", "").replace(" ", "").lower()


_pools: dict[TlsPolicy, urllib3.PoolManager] = {}


def _pool(tls: TlsPolicy) -> urllib3.PoolManager:
    pool = _pools.get(tls)
    if pool is None:
        timeout = urllib3.Timeout(connect=CONNECT_TIMEOUT, read=READ_TIMEOUT)
        if tls.fingerprint_sha256:
            # Pinning replaces chain validation: the exact certificate must match.
            pool = urllib3.PoolManager(
                cert_reqs="CERT_NONE",
                assert_fingerprint=_normalize_fingerprint(tls.fingerprint_sha256),
                timeout=timeout,
                retries=False,
            )
        else:
            context = ssl.create_default_context()
            if tls.ca_pem:
                context.load_verify_locations(cadata=tls.ca_pem)
            pool = urllib3.PoolManager(ssl_context=context, timeout=timeout, retries=False)
        _pools[tls] = pool
    return pool


def urllib3_transport(request: HttpRequest, tls: TlsPolicy) -> HttpResponse:
    try:
        response = _pool(tls).request(
            request.method, request.url, body=request.body, headers=request.headers, preload_content=True
        )
    except urllib3.exceptions.SSLError as exc:
        raise ProviderUnreachableError(f"TLS verification failed for {request.url.split('?')[0]}: {exc}") from exc
    except urllib3.exceptions.HTTPError as exc:
        raise ProviderUnreachableError(f"Cannot reach {request.url.split('?')[0]}: {exc}") from exc
    return HttpResponse(response.status, dict(response.headers), response.data)


_transport: Transport = urllib3_transport


def set_transport(transport: Transport | None) -> None:
    """Replace the network transport (tests). ``None`` restores the real one."""
    global _transport
    _transport = transport or urllib3_transport


def send(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: Mapping[str, Any] | None = None,
    json_body: Any = None,
    form: Mapping[str, Any] | None = None,
    body: bytes | None = None,
    tls: TlsPolicy | None = None,
) -> HttpResponse:
    """Send a request. Non-2xx responses are returned, not raised; see :func:`expect_ok`."""
    all_headers = {"Accept": "application/json", "User-Agent": "kubernetes-platform"}
    all_headers.update(headers or {})
    if params:
        url = f"{url}{'&' if '?' in url else '?'}{urlencode(_flatten(params), doseq=True)}"
    if json_body is not None:
        body = json.dumps(json_body).encode()
        all_headers.setdefault("Content-Type", "application/json")
    elif form is not None:
        body = urlencode(_flatten(form), doseq=True).encode()
        all_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    return _transport(HttpRequest(method, url, all_headers, body), tls or TlsPolicy())


def _flatten(values: Mapping[str, Any]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for key, value in values.items():
        if value is None:
            continue
        if isinstance(value, bool):
            out.append((key, "1" if value else "0"))
        elif isinstance(value, list | tuple):
            out.extend((key, str(v)) for v in value)
        else:
            out.append((key, str(value)))
    return out


def expect_ok(response: HttpResponse, what: str, *, message_of: Callable[[HttpResponse], str] | None = None) -> None:
    if 200 <= response.status < 300:
        return
    detail = (message_of(response) if message_of else None) or response.text[:300].strip()
    if response.status in (401, 403):
        raise ProviderAuthError(f"{what}: access denied ({response.status}) {detail}".strip(), status=response.status)
    raise ProviderApiError(f"{what} failed ({response.status}): {detail}", status=response.status)
