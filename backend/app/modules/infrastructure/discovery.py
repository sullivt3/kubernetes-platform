"""Network discovery: find Linux servers by probing address ranges for SSH.

A probe is a plain TCP connect followed by reading the SSH identification banner
(RFC 4253 §4.2, e.g. ``SSH-2.0-OpenSSH_9.6p1 Ubuntu-3ubuntu13.5``), which most
distributions customise enough to hint at the OS. Nothing is authenticated and no
credential is sent; registering a discovered host runs the normal inspection.

Guard rails: only private, loopback and link-local ranges are accepted unless the
operator enables ``DISCOVERY_ALLOW_PUBLIC_RANGES``, and a scan is capped at
``MAX_PROBES`` connection attempts.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass

from app.core.errors import ValidationFailedError

MAX_PROBES = 8192
MAX_WORKERS = 128
RANGE_RE = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3})-(\d{1,3})$")

_OS_HINTS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"ubuntu", re.I), "Ubuntu"),
    (re.compile(r"debian|deb\d+u\d+", re.I), "Debian"),
    (re.compile(r"raspbian", re.I), "Raspberry Pi OS"),
    (re.compile(r"freebsd", re.I), "FreeBSD"),
    (re.compile(r"dropbear", re.I), "Embedded Linux (Dropbear)"),
    (re.compile(r"cisco|rosssh|mikrotik", re.I), "Network device"),
    (re.compile(r"microsoft|windows", re.I), "Windows (OpenSSH)"),
    # RHEL-family OpenSSH banners carry no distribution string.
    (re.compile(r"OpenSSH_\d", re.I), "Linux/Unix (OpenSSH)"),
]


@dataclass
class ProbeResult:
    address: str
    port: int
    banner: str
    os_hint: str | None
    hostname: str | None


def parse_targets(ranges: Iterable[str], *, allow_public: bool) -> list[str]:
    """Expand CIDRs (``10.0.0.0/24``), last-octet ranges (``10.0.0.10-20``) and single IPs."""
    addresses: list[str] = []
    for raw in ranges:
        item = raw.strip()
        if not item:
            continue
        match = RANGE_RE.match(item)
        hosts: list[ipaddress.IPv4Address | ipaddress.IPv6Address]
        try:
            if match:
                start = ipaddress.IPv4Address(match.group(1))
                end_octet = int(match.group(2))
                if end_octet > 255 or end_octet < int(str(start).rsplit(".", 1)[1]):
                    raise ValueError
                base = str(start).rsplit(".", 1)[0]
                hosts = [
                    ipaddress.IPv4Address(f"{base}.{o}")
                    for o in range(int(str(start).rsplit(".", 1)[1]), end_octet + 1)
                ]
            else:
                network = ipaddress.ip_network(item, strict=False)
                if network.num_addresses > MAX_PROBES:
                    raise ValidationFailedError(f"Range {item} is too large (max {MAX_PROBES} addresses per scan)")
                hosts = list(network.hosts()) if network.num_addresses > 2 else list(network)
        except ValueError:
            raise ValidationFailedError(
                f"'{item}' is not an IP address, CIDR (10.0.0.0/24) or range (10.0.0.10-20)"
            ) from None
        for host in hosts:
            if not allow_public and not (host.is_private or host.is_loopback or host.is_link_local):
                raise ValidationFailedError(
                    f"{host} is a public address. Only private, loopback and link-local ranges can be scanned "
                    "(an administrator can allow public ranges with DISCOVERY_ALLOW_PUBLIC_RANGES)."
                )
            addresses.append(str(host))
    unique = list(dict.fromkeys(addresses))
    if not unique:
        raise ValidationFailedError("No addresses to scan")
    return unique


def os_hint(banner: str) -> str | None:
    for pattern, label in _OS_HINTS:
        if pattern.search(banner):
            return label
    return None


def probe(address: str, port: int, timeout: float) -> ProbeResult | None:
    try:
        with socket.create_connection((address, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            data = b""
            while b"\n" not in data and len(data) < 512:
                chunk = sock.recv(256)
                if not chunk:
                    break
                data += chunk
    except OSError:
        return None
    banner = next((line for line in data.decode(errors="replace").splitlines() if line.startswith("SSH-")), "")
    if not banner:
        return None  # something listens, but it is not SSH
    try:
        hostname: str | None = socket.gethostbyaddr(address)[0]
    except OSError:
        hostname = None
    return ProbeResult(address, port, banner.strip()[:255], os_hint(banner), hostname)


def scan(
    addresses: list[str],
    ports: list[int],
    *,
    timeout: float = 1.5,
    on_progress: Callable[[int, int], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> list[dict[str, object]]:
    targets = [(a, p) for a in addresses for p in ports]
    if len(targets) > MAX_PROBES:
        raise ValidationFailedError(f"Scan too large: {len(targets)} probes (max {MAX_PROBES})")
    found: list[ProbeResult] = []
    done = 0
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(targets))) as pool:
        for batch_start in range(0, len(targets), 512):
            if should_stop and should_stop():
                break
            batch = targets[batch_start : batch_start + 512]
            for result in pool.map(lambda t: probe(t[0], t[1], timeout), batch):
                if result:
                    found.append(result)
            done += len(batch)
            if on_progress:
                on_progress(done, len(targets))
    found.sort(key=lambda r: (ipaddress.ip_address(r.address), r.port))
    return [asdict(r) for r in found]
