"""Read-only operational findings with explicit source coverage and next checks."""

from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

from app.core.errors import PlatformError
from app.modules.kubernetes import client as kc
from app.modules.kubernetes.monitoring import PrometheusService

QUERIES = {
    "cpu": (
        '100 * (1 - avg by (instance,node) (rate(node_cpu_seconds_total{mode="idle"}[5m]))) > 85',
        "High CPU usage",
        "Inspect busy pods, CPU limits and node capacity.",
    ),
    "memory": (
        "100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) > 90",
        "High memory usage",
        "Inspect pod working sets, memory limits and recent OOM events.",
    ),
    "disk": (
        'max by (node,device) (100 * container_fs_usage_bytes{id="/"} / container_fs_limit_bytes{id="/"}) > 85',
        "Filesystem space running low",
        "Inspect image garbage collection, local volumes and retained logs.",
    ),
    "network": (
        'sum by (node) (max by (node,interface) (rate(container_network_receive_errors_total{id="/"}[5m])'
        ' + rate(container_network_transmit_errors_total{id="/"}[5m]))) > 0',
        "Network interface errors",
        "Check interface counters, node routes, CNI pods and recent network events.",
    ),
    "targets": (
        "up == 0",
        "Scrape target unavailable",
        "Check collector readiness, network reachability and scrape TLS.",
    ),
}
SAMPLE_METRICS = {
    "cpu": "node_cpu_seconds_total",
    "memory": "node_memory_MemAvailable_bytes",
    "disk": 'container_fs_limit_bytes{id="/"}',
    "network": 'container_network_receive_errors_total{id="/"}',
    "targets": "up",
}
SOURCES = {
    "nodes": "/api/v1/nodes",
    "pods": "/api/v1/pods",
    "deployments": "/apis/apps/v1/deployments",
    "events": "/api/v1/events",
}


def _read(api: kc.ClusterAPI, path: str) -> tuple[list[dict], str]:
    try:
        rows, truncated = kc.list_items(api, path, max_items=2000)
        return rows, "truncated" if truncated else "ok"
    except PlatformError:
        return [], "unavailable"


def collect(api: kc.ClusterAPI, connection: PrometheusService | None) -> dict[str, Any]:
    findings: list[dict] = []
    coverage = {}

    def add(
        source: str,
        name: str,
        namespace: str | None,
        title: str,
        evidence: str,
        guidance: str,
        severity: str = "warning",
    ) -> None:
        findings.append(
            dict(
                source=source,
                name=name,
                namespace=namespace,
                title=title,
                evidence=evidence,
                guidance=guidance,
                severity=severity,
            )
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {key: pool.submit(_read, api, path) for key, path in SOURCES.items()}
        inventory = {}
        for key, future in futures.items():
            rows, status = future.result()
            coverage[key] = status
            inventory[key] = rows
    for node in inventory["nodes"]:
        name = node["metadata"]["name"]
        conditions = {c["type"]: c for c in node.get("status", {}).get("conditions", [])}
        if conditions.get("Ready", {}).get("status") != "True":
            add(
                "nodes",
                name,
                None,
                "Node unavailable",
                "Node Ready condition is not True.",
                "Check the machine connection, kubelet, container runtime and control-plane reachability.",
                "critical",
            )
        for key in ("DiskPressure", "MemoryPressure", "PIDPressure", "NetworkUnavailable"):
            if conditions.get(key, {}).get("status") == "True":
                add(
                    "nodes",
                    name,
                    None,
                    key,
                    conditions[key].get("message") or key,
                    "Inspect node conditions and kubelet events. Check free disk, memory, or CNI health.",
                )
    for pod in inventory["pods"]:
        meta, pod_status = pod["metadata"], pod.get("status") or {}
        statuses = (pod_status.get("initContainerStatuses") or []) + (pod_status.get("containerStatuses") or [])
        for container in statuses:
            waiting = (container.get("state") or {}).get("waiting") or {}
            reason = waiting.get("reason")
            if reason in {"CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "CreateContainerConfigError"}:
                add(
                    "pods",
                    meta["name"],
                    meta.get("namespace"),
                    reason,
                    f"Container {container['name']} is waiting with reason {reason}.",
                    "Inspect current and previous container logs, events, image credentials and mounted configuration.",
                )
            terminated = (container.get("state") or {}).get("terminated") or {}
            if terminated.get("exitCode", 0) != 0:
                add(
                    "pods",
                    meta["name"],
                    meta.get("namespace"),
                    "Container failed",
                    f"Container {container['name']} exited with code {terminated['exitCode']} "
                    f"({terminated.get('reason', 'Unknown')}); restarts: {container.get('restartCount', 0)}.",
                    "Inspect current and previous container logs, exit reason, memory limits and recent events.",
                )
    for deployment in inventory["deployments"]:
        meta = deployment["metadata"]
        for condition in (deployment.get("status") or {}).get("conditions") or []:
            if condition.get("reason") == "ProgressDeadlineExceeded" and condition.get("status") == "False":
                add(
                    "deployments",
                    meta["name"],
                    meta.get("namespace"),
                    "Deployment rollout failed",
                    condition.get("message") or "Progress deadline exceeded.",
                    "Inspect ReplicaSets, pod events, probes, resource capacity and image pulls.",
                    "critical",
                )
    for event in inventory["events"]:
        if event.get("type") != "Warning" or event.get("reason") not in {
            "FailedMount",
            "FailedAttachVolume",
            "FailedBinding",
            "FailedCreatePodSandBox",
        }:
            continue
        ref = event.get("involvedObject") or {}
        add(
            "events",
            ref.get("name") or event["metadata"]["name"],
            ref.get("namespace"),
            event["reason"],
            (event.get("message") or "")[:500],
            "Check the resource's recent events, storage driver or CNI health and the node's connectivity.",
        )
    if connection:
        for key, (query, title, guidance) in QUERIES.items():
            try:
                data = kc.get_json(
                    api, connection.proxy_path("/api/v1/query"), {"query": query, "timeout": "5s"}, timeout=8
                )
                if data.get("status") != "success" or data["data"].get("resultType") != "vector":
                    raise ValueError("Expected vector")
                rows = data["data"]["result"]
                presence = kc.get_json(
                    api,
                    connection.proxy_path("/api/v1/query"),
                    {"query": f"count({SAMPLE_METRICS[key]})", "timeout": "5s"},
                    timeout=8,
                )
                counts = presence["data"]["result"]
                coverage[key] = "ok" if counts and float(counts[0]["value"][1]) > 0 else "no_samples"
                for row in rows[:50]:
                    labels = row["metric"]
                    value = float(row["value"][1])
                    if math.isfinite(value):
                        add(
                            key,
                            labels.get("node") or labels.get("instance") or key,
                            None,
                            title,
                            f"Observed {value:.2f}. "
                            + ", ".join(f"{k}={v}" for k, v in labels.items() if k in {"job", "mountpoint", "device"}),
                            guidance,
                        )
            except (PlatformError, ValueError, KeyError, TypeError, IndexError):
                coverage[key] = "unavailable"
    else:
        coverage.update(dict.fromkeys(QUERIES, "not_configured"))
    try:
        code, _ = api.request("GET", "/readyz", timeout=8)
        coverage["api"] = "ok"
        if code != 200:
            add(
                "api",
                "API server",
                None,
                "API readiness failed",
                f"Readiness returned HTTP {code}.",
                "Inspect API server readiness checks, etcd health and control-plane logs.",
                "critical",
            )
    except PlatformError:
        coverage["api"] = "unavailable"
        add(
            "api",
            "API server",
            None,
            "API server unreachable",
            "The registered API connection did not complete its readiness request.",
            "Check control-plane reachability, credentials, TLS trust and API server logs.",
            "critical",
        )
    rules = []
    if connection:
        try:
            payload = kc.get_json(api, connection.proxy_path("/api/v1/rules"), {"type": "alert"}, timeout=8)
            if payload.get("status") != "success":
                raise ValueError("Expected rule list")
            for group in payload["data"]["groups"]:
                for rule in group["rules"]:
                    rules.append(
                        {
                            key: rule.get(key)
                            for key in ("name", "state", "health", "duration", "query", "lastError", "annotations")
                        }
                    )
            coverage["rules"] = "ok"
        except (PlatformError, ValueError, KeyError, TypeError):
            coverage["rules"] = "unavailable"
    findings.sort(key=lambda f: (f["severity"] != "critical", f["source"], f["namespace"] or "", f["name"]))
    return {"observed_at": datetime.now(UTC), "coverage": coverage, "findings": findings, "rules": rules}
