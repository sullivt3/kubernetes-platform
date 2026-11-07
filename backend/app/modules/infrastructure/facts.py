"""Parse machine inspection output and evaluate Kubernetes prerequisites."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from functools import cache
from importlib import resources
from typing import Any


@cache
def detect_script() -> str:
    return (
        (resources.files("app.modules.infrastructure") / "scripts" / "detect.sh")
        .read_text()
        .replace("\r\n", "\n")
    )


def parse_facts(output: str) -> dict[str, str]:
    facts: dict[str, str] = {}
    for line in output.splitlines():
        key, sep, value = line.partition("=")
        if sep and key and key.replace("_", "").isalnum():
            facts[key.strip()] = value.strip()
    return facts


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except ValueError:
        return None


# Supported operating systems for the kubeadm installer: (os id, major versions).
SUPPORTED_OS: dict[str, set[str]] = {
    "ubuntu": {"22.04", "24.04"},
    "debian": {"12", "13"},
    "rhel": {"8", "9"},
    "rocky": {"8", "9"},
    "almalinux": {"8", "9"},
}
SUPPORTED_ARCH = {"x86_64", "aarch64"}
MIN_MEMORY_MB = 1700  # kubeadm requires ~2 GB; leave room for firmware reservations
MIN_CPUS_CONTROL_PLANE = 2


class CheckStatus(StrEnum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True)
class Check:
    key: str
    label: str
    status: CheckStatus
    message: str


def os_supported(facts: dict[str, str]) -> bool:
    os_id = facts.get("os_id", "")
    version = facts.get("os_version_id", "")
    versions = SUPPORTED_OS.get(os_id)
    if versions is None:
        return False
    return version in versions or version.split(".")[0] in versions


def evaluate(facts: dict[str, str], *, sudo_ok: bool) -> list[Check]:
    checks: list[Check] = []

    def add(key: str, label: str, status: CheckStatus, message: str) -> None:
        checks.append(Check(key, label, status, message))

    pretty = facts.get("os_pretty_name") or facts.get("os_id", "unknown")
    if os_supported(facts):
        add("os", "Operating system", CheckStatus.PASS, f"{pretty} is supported")
    else:
        supported = ", ".join(f"{k} {'/'.join(sorted(v))}" for k, v in SUPPORTED_OS.items())
        add("os", "Operating system", CheckStatus.FAIL, f"{pretty} is not supported (supported: {supported})")

    arch = facts.get("arch", "")
    add(
        "arch",
        "CPU architecture",
        CheckStatus.PASS if arch in SUPPORTED_ARCH else CheckStatus.FAIL,
        f"{arch or 'unknown'}" + ("" if arch in SUPPORTED_ARCH else " is not supported (x86_64 or aarch64)"),
    )

    add(
        "systemd",
        "systemd",
        CheckStatus.PASS if facts.get("systemd") == "yes" else CheckStatus.FAIL,
        "systemd is the init system"
        if facts.get("systemd") == "yes"
        else "systemd is required to run kubelet",
    )
    add(
        "sudo",
        "Administrative access",
        CheckStatus.PASS if sudo_ok else CheckStatus.FAIL,
        "root or non-interactive sudo is available"
        if sudo_ok
        else "the SSH user needs root or sudo rights (passwordless sudo for key-based logins)",
    )

    cpus = _int(facts.get("cpu_cores")) or 0
    add(
        "cpu",
        "CPU",
        CheckStatus.PASS if cpus >= MIN_CPUS_CONTROL_PLANE else CheckStatus.WARN,
        f"{cpus} core(s)"
        + (
            ""
            if cpus >= MIN_CPUS_CONTROL_PLANE
            else "; control-plane nodes need at least 2 (workers may use 1)"
        ),
    )
    memory = _int(facts.get("memory_mb")) or 0
    add(
        "memory",
        "Memory",
        CheckStatus.PASS if memory >= MIN_MEMORY_MB else CheckStatus.FAIL,
        f"{memory} MB" + ("" if memory >= MIN_MEMORY_MB else f"; at least {MIN_MEMORY_MB} MB is required"),
    )
    swap = _int(facts.get("swap_mb")) or 0
    add(
        "swap",
        "Swap",
        CheckStatus.PASS if swap == 0 else CheckStatus.WARN,
        "disabled"
        if swap == 0
        else f"{swap} MB enabled; it is disabled during installation unless the cluster allows swap",
    )
    free_disk = _int(facts.get("root_disk_free_gb"))
    if free_disk is not None:
        add(
            "disk",
            "Disk space",
            CheckStatus.PASS if free_disk >= 10 else CheckStatus.WARN,
            f"{free_disk} GB free on /" + ("" if free_disk >= 10 else "; at least 10 GB is recommended"),
        )
    if not facts.get("primary_ip"):
        add("network", "Network", CheckStatus.FAIL, "no default route / primary IPv4 address detected")
    else:
        add("network", "Network", CheckStatus.PASS, f"primary address {facts['primary_ip']}")
    internet = facts.get("internet", "")
    add(
        "internet",
        "Package repositories",
        CheckStatus.PASS if internet.isdigit() and internet != "000" else CheckStatus.WARN,
        "pkgs.k8s.io is reachable"
        if internet.isdigit() and internet != "000"
        else (
            "pkgs.k8s.io is not reachable; installation needs access to pkgs.k8s.io, "
            "download.docker.com and registry.k8s.io"
        ),
    )
    nameservers = facts.get("nameservers", "").split()
    loopback_only = bool(nameservers) and all(ns.startswith("127.") or ns == "::1" for ns in nameservers)
    if loopback_only and facts.get("systemd_resolved") != "active":
        add(
            "dns",
            "DNS resolvers",
            CheckStatus.WARN,
            f"/etc/resolv.conf only lists loopback nameservers ({', '.join(nameservers)}); cluster DNS would "
            "forward to itself. Set the cluster's kubelet resolv.conf option to a file with real "
            "upstream servers",
        )
    elif nameservers:
        add("dns", "DNS resolvers", CheckStatus.PASS, ", ".join(nameservers))
    if facts.get("has_kubelet_conf") == "yes" or facts.get("has_admin_conf") == "yes":
        add(
            "existing_cluster",
            "Existing Kubernetes",
            CheckStatus.WARN,
            "this machine already belongs to a Kubernetes cluster; it must be reset before joining a new one",
        )
    return checks


def summarize(facts: dict[str, str]) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, str]]]:
    """Split raw facts into (os_info, hardware, addresses) as stored on the machine."""
    os_info = {
        "id": facts.get("os_id"),
        "id_like": facts.get("os_id_like"),
        "version": facts.get("os_version_id"),
        "pretty_name": facts.get("os_pretty_name"),
        "kernel": facts.get("kernel"),
        "hostname": facts.get("hostname"),
        "package_manager": facts.get("package_manager"),
        "cgroup_version": facts.get("cgroup_version"),
        "selinux": facts.get("selinux"),
        "virtualization": facts.get("virtualization"),
        "kubernetes": {
            "kubeadm": facts.get("kubeadm_version") or None,
            "kubelet": facts.get("kubelet_version") or None,
            "containerd": facts.get("containerd_version") or None,
            "member_of_cluster": facts.get("has_kubelet_conf") == "yes",
        },
    }
    hardware = {
        "architecture": facts.get("arch"),
        "cpu_cores": _int(facts.get("cpu_cores")),
        "memory_mb": _int(facts.get("memory_mb")),
        "swap_mb": _int(facts.get("swap_mb")),
        "root_disk_gb": _int(facts.get("root_disk_gb")),
        "root_disk_free_gb": _int(facts.get("root_disk_free_gb")),
    }
    addresses = [{"type": "primary", "address": facts["primary_ip"]}] if facts.get("primary_ip") else []
    for ip in facts.get("all_ipv4", "").split():
        if ip != facts.get("primary_ip"):
            addresses.append({"type": "internal", "address": ip})
    return os_info, hardware, addresses


def checks_to_json(checks: list[Check]) -> list[dict[str, str]]:
    return [{k: str(v) for k, v in asdict(c).items()} for c in checks]
