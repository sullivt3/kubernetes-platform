"""Minimal, secure Kubernetes REST client.

The platform talks to the Kubernetes API over HTTPS with urllib3:

* The server certificate is always verified against the cluster's CA (in memory).
* Client keys are loaded into an in-memory SSL context; the temporary files the
  ``ssl`` module requires exist only for the duration of loading (0600, private dir).
* ``tls_server_name`` lets the platform reach an API server through a different
  address (NAT, proxy, load balancer) while still verifying its real identity.
* Every request has connect/read timeouts; failures map to explicit errors so an
  unreachable cluster never hangs or breaks the management application.

Tests replace the transport with :func:`set_client_factory`.
"""

from __future__ import annotations

import json
import os
import shutil
import ssl
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlencode, urlsplit

import urllib3

from app.core.errors import PlatformError
from app.modules.kubernetes.kubeconfig import ResolvedKubeconfig


class ClusterUnreachableError(PlatformError):
    status_code = 503
    code = "cluster_unreachable"


class ClusterAuthError(PlatformError):
    status_code = 502
    code = "cluster_auth_failed"


class KubernetesAPIError(PlatformError):
    status_code = 502
    code = "kubernetes_api_error"

    def __init__(self, message: str, *, http_status: int | None = None, details: dict[str, Any] | None = None):
        super().__init__(message, details=details)
        self.http_status = http_status


class NotFoundInClusterError(KubernetesAPIError):
    status_code = 404
    code = "not_found_in_cluster"


@dataclass(frozen=True)
class ConnectionTarget:
    """Everything needed to reach one cluster's API server."""

    kubeconfig: ResolvedKubeconfig
    server_override: str | None = None
    tls_server_name: str | None = None
    token_provider: Callable[[], str] | None = None

    @property
    def server(self) -> str:
        return (self.server_override or self.kubeconfig.server).rstrip("/")

    @property
    def verify_name(self) -> str | None:
        return self.tls_server_name or self.kubeconfig.tls_server_name


class ClusterAPI(Protocol):
    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: Any = None,
        content_type: str = "application/json",
        timeout: float | None = None,
    ) -> tuple[int, bytes]: ...

    def close(self) -> None: ...


def _ssl_context(target: ConnectionTarget) -> ssl.SSLContext:
    context = ssl.create_default_context(cadata=target.kubeconfig.ca_pem)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    # Python 3.13+ enables RFC 5280 strict mode by default, which rejects cluster CAs that
    # omit e.g. the Authority Key Identifier. kubectl accepts them; so do we. The chain is
    # still verified against the cluster CA and the hostname/IP is still checked.
    context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    if target.kubeconfig.client_cert_pem and target.kubeconfig.client_key_pem:
        directory = tempfile.mkdtemp(prefix="kp-tls-")
        try:
            cert_path, key_path = os.path.join(directory, "c.pem"), os.path.join(directory, "k.pem")
            for path, content in (
                (cert_path, target.kubeconfig.client_cert_pem),
                (key_path, target.kubeconfig.client_key_pem),
            ):
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as handle:
                    handle.write(content)
            context.load_cert_chain(cert_path, key_path)
        finally:
            shutil.rmtree(directory, ignore_errors=True)
    return context


class KubeClient:
    def __init__(self, target: ConnectionTarget, *, connect_timeout: float = 5.0, read_timeout: float = 20.0) -> None:
        self.target = target
        parts = urlsplit(target.server)
        if parts.scheme != "https" or not parts.hostname:
            raise ClusterUnreachableError(f"Invalid API server URL '{target.server}'")
        self._base = target.server
        self._token = target.kubeconfig.token
        self._timeout = urllib3.Timeout(connect=connect_timeout, read=read_timeout)
        try:
            context = _ssl_context(target)
        except ssl.SSLError as exc:
            raise ClusterAuthError(f"Invalid TLS material in kubeconfig: {exc}") from exc
        pool_kwargs: dict[str, Any] = {
            "ssl_context": context,
            "retries": False,
            "maxsize": 8,
            "timeout": self._timeout,
        }
        if target.verify_name:
            pool_kwargs["server_hostname"] = target.verify_name
            pool_kwargs["assert_hostname"] = target.verify_name
        self._pool = urllib3.PoolManager(num_pools=2, **pool_kwargs)

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: Any = None,
        content_type: str = "application/json",
        timeout: float | None = None,
    ) -> tuple[int, bytes]:
        url = self._base + path
        if params:
            url += ("&" if "?" in url else "?") + urlencode({k: v for k, v in params.items() if v is not None})
        headers = {"Accept": "application/json", "User-Agent": "kubernetes-platform/0.1"}
        token = self.target.token_provider() if self.target.token_provider else self._token
        if token:
            headers["Authorization"] = f"Bearer {token}"
        payload = None
        if body is not None:
            headers["Content-Type"] = content_type
            payload = body if isinstance(body, bytes) else json.dumps(body).encode()
        try:
            response = self._pool.request(
                method,
                url,
                body=payload,
                headers=headers,
                preload_content=True,
                timeout=urllib3.Timeout(connect=self._timeout.connect_timeout, read=timeout) if timeout else None,
            )
        except urllib3.exceptions.SSLError as exc:
            raise ClusterUnreachableError(
                f"TLS verification of {self.target.server} failed: {exc.reason if hasattr(exc, 'reason') else exc}. "
                "If you reach the API through a different address, set the TLS server name to the name/IP in the "
                "API server certificate.",
            ) from exc
        except urllib3.exceptions.HTTPError as exc:
            raise ClusterUnreachableError(f"Cannot reach the Kubernetes API at {self.target.server}: {exc}") from exc
        if response.status in (401, 403):
            raise ClusterAuthError(
                f"The Kubernetes API rejected the platform's credentials (HTTP {response.status})",
                details={"path": path},
            )
        return response.status, response.data

    def stream(self, path: str, params: dict[str, Any] | None = None, *, max_seconds: float = 900) -> Any:
        """Yield decoded lines from a long-lived response (e.g. pod logs with follow=true)."""
        url = self._base + path
        if params:
            url += "?" + urlencode({k: v for k, v in params.items() if v is not None})
        headers = {"Accept": "*/*", "User-Agent": "kubernetes-platform/0.1"}
        token = self.target.token_provider() if self.target.token_provider else self._token
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            response = self._pool.request(
                "GET",
                url,
                headers=headers,
                preload_content=False,
                timeout=urllib3.Timeout(connect=self._timeout.connect_timeout, read=max_seconds),
            )
        except urllib3.exceptions.HTTPError as exc:
            raise ClusterUnreachableError(f"Cannot reach the Kubernetes API at {self.target.server}: {exc}") from exc
        if response.status >= 400:
            data = response.read()
            response.release_conn()
            if response.status in (401, 403):
                raise ClusterAuthError(f"The Kubernetes API rejected the request (HTTP {response.status})")
            _raise_for(response.status, data, path)
        started = time.monotonic()
        buffer = b""
        try:
            for chunk in response.stream(4096):
                buffer += chunk
                *lines, buffer = buffer.split(b"\n")
                for line in lines:
                    yield line.decode(errors="replace")
                if time.monotonic() - started > max_seconds:
                    break
            if buffer:
                yield buffer.decode(errors="replace")
        except (urllib3.exceptions.ReadTimeoutError, urllib3.exceptions.ProtocolError):
            return
        finally:
            response.release_conn()

    def close(self) -> None:
        self._pool.clear()


# --------------------------------------------------------------------------- helpers over ClusterAPI


def _error_message(data: bytes) -> str:
    try:
        return json.loads(data).get("message") or data.decode(errors="replace")[:300]
    except (ValueError, AttributeError):
        return data.decode(errors="replace")[:300]


def get_json(api: ClusterAPI, path: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> Any:
    status, data = api.request("GET", path, params=params, timeout=timeout)
    if status == 404:
        raise NotFoundInClusterError(f"Not found in cluster: {path}", http_status=404)
    if status >= 400:
        raise KubernetesAPIError(f"Kubernetes API error ({status}): {_error_message(data)}", http_status=status)
    return json.loads(data)


def list_items(
    api: ClusterAPI,
    path: str,
    params: dict[str, Any] | None = None,
    *,
    max_items: int = 5000,
    page_size: int = 500,
) -> tuple[list[dict[str, Any]], bool]:
    """List with pagination. Returns (items, truncated)."""
    items: list[dict[str, Any]] = []
    query = dict(params or {}, limit=page_size)
    while True:
        page = get_json(api, path, query)
        items.extend(page.get("items", []))
        token = (page.get("metadata") or {}).get("continue")
        if not token:
            return items, False
        if len(items) >= max_items:
            return items[:max_items], True
        query["continue"] = token


# --------------------------------------------------------------------------- factory (cached, replaceable)

ClientFactory = Callable[[ConnectionTarget], ClusterAPI]
_CACHE_SECONDS = 300
_cache: dict[tuple[Any, ...], tuple[float, ClusterAPI]] = {}
_lock = threading.Lock()


def _default_factory(target: ConnectionTarget) -> ClusterAPI:
    return KubeClient(target)


_factory: ClientFactory = _default_factory


def set_client_factory(factory: ClientFactory | None) -> None:
    """Replace the transport (tests). ``None`` restores the HTTPS client."""
    global _factory
    _factory = factory or _default_factory
    with _lock:
        _cache.clear()


def client_for(cache_key: tuple[Any, ...], make_target: Callable[[], ConnectionTarget]) -> ClusterAPI:
    """Pooled client per cluster; rebuilt when the credential or connection settings change.

    ``make_target`` (which decrypts the kubeconfig) only runs on a cache miss.
    """
    now = time.monotonic()
    with _lock:
        cached = _cache.get(cache_key)
        if cached and now - cached[0] < _CACHE_SECONDS:
            return cached[1]
    client = _factory(make_target())
    with _lock:
        # Drop every client for the same cluster (key[0]); settings or credentials may have changed.
        stale = [k for k in _cache if k[0] == cache_key[0]]
        old = [_cache.pop(k)[1] for k in stale]
        _cache[cache_key] = (now, client)
    for previous in old:
        previous.close()
    return client


def forget(cluster_id: Any) -> None:
    with _lock:
        stale = [k for k in _cache if k[0] == cluster_id]
        old = [_cache.pop(k)[1] for k in stale]
    for previous in old:
        previous.close()


# --------------------------------------------------------------------------- writes & streaming

MERGE_PATCH = "application/merge-patch+json"
JSON_PATCH = "application/json-patch+json"
STRATEGIC_PATCH = "application/strategic-merge-patch+json"


def _raise_for(status: int, data: bytes, path: str) -> None:
    if status < 400:
        return
    try:
        payload = json.loads(data)
    except ValueError:
        payload = {}
    message = payload.get("message") or data.decode(errors="replace")[:300]
    causes = [
        {"field": c.get("field"), "message": c.get("message"), "reason": c.get("reason")}
        for c in ((payload.get("details") or {}).get("causes") or [])
    ]
    if status == 404:
        raise NotFoundInClusterError(message or f"Not found: {path}", http_status=404)
    raise KubernetesAPIError(
        f"Kubernetes rejected the request ({status}): {message}",
        http_status=status,
        details={"reason": payload.get("reason"), "causes": causes} if causes or payload.get("reason") else None,
    )


def send_json(
    api: ClusterAPI,
    method: str,
    path: str,
    body: Any = None,
    *,
    content_type: str = "application/json",
    params: dict[str, Any] | None = None,
) -> Any:
    status, data = api.request(method, path, params=params, body=body, content_type=content_type)
    _raise_for(status, data, path)
    return json.loads(data) if data else None


def get_text(api: ClusterAPI, path: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> str:
    status, data = api.request("GET", path, params=params, timeout=timeout)
    _raise_for(status, data, path)
    return data.decode(errors="replace")


def stream_lines(api: ClusterAPI, path: str, params: dict[str, Any] | None = None) -> Any:
    """Iterate text lines from a streaming endpoint (pod logs with follow=true)."""
    streamer = getattr(api, "stream", None)
    if streamer is None:
        raise KubernetesAPIError("Streaming is not supported by this connection")
    return streamer(path, params)
