"""DNS-pinned TLS requests; credentials and custom trust stay bound to their origin."""

from __future__ import annotations

import ipaddress
import socket
import ssl
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import urllib3

from app.core.errors import PlatformError


class CatalogError(PlatformError):
    status_code = 502
    code = "helm_catalog_error"


def origin(url: str) -> tuple[str, str, int]:
    parsed = urlsplit(url.replace("oci://", "https://", 1))
    return "https", (parsed.hostname or "").encode("idna").decode(), parsed.port or 443


def check_url(value: str, *, repository: bool = False, private: bool = False) -> str:
    try:
        parsed = urlsplit(value)
        schemes = {"https", "oci"} if repository else {"https"}
        if (
            parsed.scheme not in schemes
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or (not private and parsed.port not in {None, 443})
            or (repository and parsed.query)
            or len(value) > 4096
            or "\\" in value
        ):
            raise ValueError()
        if parsed.scheme == "oci":
            import re

            if not re.fullmatch(r"/[a-z0-9][a-z0-9._/-]*", parsed.path) or any(
                p in {"", ".", ".."} for p in parsed.path[1:].split("/")
            ):
                raise ValueError()
    except ValueError:
        raise ValueError(
            "Use an HTTPS repository or full oci:// chart URL without credentials, query tokens or fragments"
        ) from None
    return value.rstrip("/") if repository else value


def permitted_address(value: str, private: bool) -> bool:
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    if address.is_global and not address.is_multicast:
        return True
    if not private:
        return False
    networks = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"] if address.version == 4 else ["fc00::/7"]
    return any(address in ipaddress.ip_network(network) for network in networks)


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    data: bytes


def request(
    url: str,
    limit: int,
    *,
    policy=None,
    authorization: str | None = None,
    headers: dict[str, str] | None = None,
    credential_origin: tuple[str, str, int] | None = None,
    redirects: bool = True,
) -> Response:
    trusted = origin(policy.url) if policy else origin(url)
    credential_origin = credential_origin or trusted
    send_auth = origin(url) == credential_origin
    for _ in range(5):
        try:
            same = origin(url) == trusted
            allow_private = bool(policy and policy.allow_private_network and same)
            check_url(url, private=allow_private)
            parsed = urlsplit(url)
            host = (parsed.hostname or "").encode("idna").decode()
            port = parsed.port or 443
            addresses = sorted({str(r[4][0]) for r in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
            if not addresses or any(not permitted_address(address, allow_private) for address in addresses):
                raise ValueError()
            context = ssl.create_default_context()
            if same and policy and policy.ca_pem:
                context.load_verify_locations(cadata=policy.ca_pem)
            pool = urllib3.HTTPSConnectionPool(
                addresses[0], port=port, server_hostname=host, assert_hostname=host, ssl_context=context
            )
            try:
                path = parsed.path or "/"
                if parsed.query:
                    path += "?" + parsed.query
                supplied = {k: v for k, v in (headers or {}).items() if k.lower() not in {"authorization", "host"}}
                supplied["Host"] = host if port == 443 else (f"[{host}]:{port}" if ":" in host else f"{host}:{port}")
                if send_auth and origin(url) == credential_origin and authorization:
                    supplied["Authorization"] = authorization
                response = pool.urlopen(
                    "GET",
                    path,
                    headers=supplied,
                    redirect=False,
                    retries=False,
                    preload_content=False,
                    timeout=urllib3.Timeout(connect=5, read=20),
                )
                try:
                    if redirects and response.status in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location")
                        if not location:
                            raise ValueError()
                        next_url = urljoin(url, location)
                        if origin(next_url) != origin(url):
                            send_auth = False
                        url = next_url
                        continue
                    data = response.read(limit + 1)
                    if len(data) > limit:
                        raise ValueError()
                    return Response(response.status, {k.lower(): v for k, v in response.headers.items()}, data)
                finally:
                    response.close()
            finally:
                pool.close()
        except (OSError, ValueError, urllib3.exceptions.HTTPError):
            raise CatalogError(
                "Repository request failed. Check endpoint policy, TLS trust, reachability and download limits."
            ) from None
    raise CatalogError("Repository request exceeded the redirect limit")
