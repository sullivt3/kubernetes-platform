"""Supported Kubernetes versions, discovered from the official release channel (dl.k8s.io)."""

from __future__ import annotations

import logging
import re
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from app.modules.cluster_lifecycle.distributions.kubeadm.config import MIN_MINOR

logger = logging.getLogger(__name__)

RELEASE_BASE = "https://dl.k8s.io/release"
SUPPORTED_MINORS = 3  # upstream supports the three most recent minor releases
CACHE_SECONDS = 3600
_VERSION = re.compile(r"^v1\.(\d+)\.(\d+)$")


@dataclass
class VersionCatalog:
    available: bool
    latest: str | None = None
    versions: list[dict[str, str]] = field(default_factory=list)
    minimum_minor: str = f"1.{MIN_MINOR}"
    message: str | None = None


_cache: tuple[float, VersionCatalog] | None = None
_lock = threading.Lock()


def _fetch(path: str) -> str:
    with urllib.request.urlopen(f"{RELEASE_BASE}/{path}", timeout=5) as response:
        return response.read().decode().strip()


def _load() -> VersionCatalog:
    try:
        latest = _fetch("stable.txt")
        match = _VERSION.match(latest)
        if not match:
            raise ValueError(f"unexpected stable version '{latest}'")
        newest_minor = int(match.group(1))
        minors = [m for m in range(newest_minor, newest_minor - SUPPORTED_MINORS, -1) if m >= MIN_MINOR]
        with ThreadPoolExecutor(max_workers=len(minors)) as pool:
            patches = list(pool.map(lambda m: _fetch(f"stable-1.{m}.txt"), minors))
        versions = [
            {"minor": f"1.{m}", "latest_patch": p.lstrip("v")}
            for m, p in zip(minors, patches, strict=True)
            if _VERSION.match(p)
        ]
        return VersionCatalog(available=True, latest=latest.lstrip("v"), versions=versions)
    except Exception as exc:  # network failure, proxy, unexpected content
        logger.warning("Could not load Kubernetes release information: %s", exc)
        return VersionCatalog(
            available=False,
            message="Release information from dl.k8s.io is unavailable; enter a version (e.g. 1.34).",
        )


def kubernetes_versions() -> VersionCatalog:
    global _cache
    with _lock:
        if _cache and time.monotonic() - _cache[0] < CACHE_SECONDS:
            return _cache[1]
    catalog = _load()
    with _lock:
        # Cache failures briefly only, so a transient outage doesn't stick for an hour.
        _cache = (time.monotonic() - (CACHE_SECONDS - 60 if not catalog.available else 0), catalog)
    return catalog
