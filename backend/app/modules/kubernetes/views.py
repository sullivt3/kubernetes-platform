"""Summaries of live Kubernetes objects for the API/UI (pure functions over raw JSON)."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from app.modules.cluster_lifecycle.distributions.types import DistributionType
from app.modules.kubernetes.quantities import cpu_millicores, memory_bytes

Obj = dict[str, Any]
PROBLEM_REASONS = {
    "CrashLoopBackOff",
    "ImagePullBackOff",
    "ErrImagePull",
    "CreateContainerConfigError",
    "CreateContainerError",
    "InvalidImageName",
    "RunContainerError",
    "OOMKilled",
    "Error",
    "ContainerStatusUnknown",
    "Evicted",
}


def _meta(obj: Obj) -> Obj:
    return obj.get("metadata") or {}


# --------------------------------------------------------------------------- pods


def pod_status(pod: Obj) -> str:
    """Approximates the STATUS column of `kubectl get pods`."""
    meta, status = _meta(pod), pod.get("status") or {}
    if meta.get("deletionTimestamp"):
        return "Terminating"
    reason = status.get("reason")
    if reason:  # e.g. Evicted
        return reason
    for container in status.get("initContainerStatuses") or []:
        state = container.get("state") or {}
        if "terminated" in state and state["terminated"].get("exitCode", 0) != 0:
            return f"Init:{state['terminated'].get('reason') or 'Error'}"
        if "waiting" in state and state["waiting"].get("reason") not in (None, "PodInitializing"):
            return f"Init:{state['waiting']['reason']}"
    for container in status.get("containerStatuses") or []:
        state = container.get("state") or {}
        if "waiting" in state and state["waiting"].get("reason"):
            return state["waiting"]["reason"]
        if "terminated" in state and state["terminated"].get("reason"):
            return state["terminated"]["reason"]
    return status.get("phase") or "Unknown"


def pod_requests(pod: Obj) -> tuple[int, int]:
    """(cpu millicores, memory bytes) requested by a pod (max of init vs sum of app containers)."""
    spec = pod.get("spec") or {}

    def requested(containers: list[Obj]) -> list[tuple[int, int]]:
        return [
            (
                cpu_millicores(((c.get("resources") or {}).get("requests") or {}).get("cpu")),
                memory_bytes(((c.get("resources") or {}).get("requests") or {}).get("memory")),
            )
            for c in containers
        ]

    app = requested(spec.get("containers") or [])
    init = requested(spec.get("initContainers") or [])
    cpu = max(sum(c for c, _ in app), max((c for c, _ in init), default=0))
    mem = max(sum(m for _, m in app), max((m for _, m in init), default=0))
    return cpu, mem


def pod_view(pod: Obj) -> Obj:
    meta, spec, status = _meta(pod), pod.get("spec") or {}, pod.get("status") or {}
    containers = status.get("containerStatuses") or []
    owner: Obj = next(iter(meta.get("ownerReferences") or []), {})
    shown = pod_status(pod)
    return {
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "phase": status.get("phase"),
        "status": shown,
        "problem": shown in PROBLEM_REASONS or shown.removeprefix("Init:") in PROBLEM_REASONS,
        "ready": f"{sum(1 for c in containers if c.get('ready'))}/{len(spec.get('containers') or [])}",
        "restarts": sum(int(c.get("restartCount") or 0) for c in containers),
        "node": spec.get("nodeName"),
        "pod_ip": status.get("podIP"),
        "created": meta.get("creationTimestamp"),
        "owner_kind": owner.get("kind"),
        "owner_name": owner.get("name"),
        "images": [c.get("image") for c in spec.get("containers") or []],
        "qos_class": status.get("qosClass"),
    }


def _active(pod: Obj) -> bool:
    return (pod.get("status") or {}).get("phase") not in ("Succeeded", "Failed")


# --------------------------------------------------------------------------- nodes


def node_roles(node: Obj) -> list[str]:
    roles = sorted(
        key.split("/", 1)[1]
        for key in (_meta(node).get("labels") or {})
        if key.startswith("node-role.kubernetes.io/") and key.split("/", 1)[1]
    )
    return roles or ["<none>"]


def node_view(node: Obj, pods: list[Obj], usage: Obj | None) -> Obj:
    meta, spec, status = _meta(node), node.get("spec") or {}, node.get("status") or {}
    conditions = {c.get("type"): c for c in status.get("conditions") or []}
    ready = conditions.get("Ready", {}).get("status") == "True"
    pressure = [
        t
        for t in ("MemoryPressure", "DiskPressure", "PIDPressure", "NetworkUnavailable")
        if conditions.get(t, {}).get("status") == "True"
    ]
    info = status.get("nodeInfo") or {}
    addresses = {a.get("type"): a.get("address") for a in status.get("addresses") or []}
    allocatable, capacity = status.get("allocatable") or {}, status.get("capacity") or {}
    active = [p for p in pods if _active(p)]
    requested = [pod_requests(p) for p in active]
    return {
        "name": meta.get("name"),
        "roles": node_roles(node),
        "ready": ready,
        "status": ("Ready" if ready else "NotReady") + (",SchedulingDisabled" if spec.get("unschedulable") else ""),
        "problems": pressure,
        "unschedulable": bool(spec.get("unschedulable")),
        "kubelet_version": info.get("kubeletVersion"),
        "os_image": info.get("osImage"),
        "kernel": info.get("kernelVersion"),
        "container_runtime": info.get("containerRuntimeVersion"),
        "architecture": info.get("architecture"),
        "internal_ip": addresses.get("InternalIP"),
        "external_ip": addresses.get("ExternalIP"),
        "created": meta.get("creationTimestamp"),
        "labels": meta.get("labels") or {},
        "taints": [
            {"key": t.get("key"), "value": t.get("value"), "effect": t.get("effect")} for t in spec.get("taints") or []
        ],
        "cpu": {
            "capacity_m": cpu_millicores(capacity.get("cpu")),
            "allocatable_m": cpu_millicores(allocatable.get("cpu")),
            "requested_m": sum(c for c, _ in requested),
            "used_m": usage.get("cpu_m") if usage else None,
        },
        "memory": {
            "capacity_bytes": memory_bytes(capacity.get("memory")),
            "allocatable_bytes": memory_bytes(allocatable.get("memory")),
            "requested_bytes": sum(m for _, m in requested),
            "used_bytes": usage.get("memory_bytes") if usage else None,
        },
        "pods": {"count": len(active), "capacity": int(allocatable.get("pods") or 0)},
        "conditions": [
            {
                "type": c.get("type"),
                "status": c.get("status"),
                "reason": c.get("reason"),
                "message": c.get("message"),
                "last_transition": c.get("lastTransitionTime"),
            }
            for c in status.get("conditions") or []
        ],
    }


def node_usage(metrics: Obj | None) -> dict[str, Obj]:
    if not metrics:
        return {}
    return {
        str(_meta(item).get("name")): {
            "cpu_m": cpu_millicores((item.get("usage") or {}).get("cpu")),
            "memory_bytes": memory_bytes((item.get("usage") or {}).get("memory")),
        }
        for item in metrics.get("items", [])
    }


def pods_by_node(pods: list[Obj]) -> dict[str, list[Obj]]:
    grouped: dict[str, list[Obj]] = defaultdict(list)
    for pod in pods:
        grouped[(pod.get("spec") or {}).get("nodeName") or ""].append(pod)
    return grouped


# --------------------------------------------------------------------------- namespaces & events


def namespace_view(ns: Obj, pods: list[Obj]) -> Obj:
    meta = _meta(ns)
    phases = Counter((p.get("status") or {}).get("phase") or "Unknown" for p in pods)
    return {
        "name": meta.get("name"),
        "status": (ns.get("status") or {}).get("phase"),
        "created": meta.get("creationTimestamp"),
        "labels": meta.get("labels") or {},
        "pods": {"total": len(pods), **{k.lower(): v for k, v in phases.items()}},
        "problem_pods": sum(1 for p in pods if pod_view(p)["problem"]),
    }


def event_view(event: Obj) -> Obj:
    meta, involved = _meta(event), event.get("involvedObject") or event.get("regarding") or {}
    return {
        "namespace": meta.get("namespace"),
        "type": event.get("type"),
        "reason": event.get("reason"),
        "message": event.get("message") or event.get("note"),
        "object": f"{involved.get('kind', '?')}/{involved.get('name', '?')}",
        "count": event.get("count") or (event.get("series") or {}).get("count") or 1,
        "last_seen": event.get("lastTimestamp") or event.get("eventTime") or meta.get("creationTimestamp"),
        "source": (event.get("source") or {}).get("component") or event.get("reportingController"),
    }


# --------------------------------------------------------------------------- cluster summary


def summarize(
    version: Obj, ready: tuple[bool, list[Obj]], nodes: list[Obj], pods: list[Obj], metrics: Obj | None
) -> Obj:
    """Cluster health summary (stored by the periodic probe and shown on the dashboard)."""
    usage = node_usage(metrics)
    by_node = pods_by_node(pods)
    node_views = [
        node_view(n, by_node.get(str(_meta(n).get("name")), []), usage.get(str(_meta(n).get("name")))) for n in nodes
    ]
    pod_views = [pod_view(p) for p in pods]
    phases = Counter(p["phase"] or "Unknown" for p in pod_views)

    def total(resource: str, field: str) -> int:
        return sum(n[resource][field] or 0 for n in node_views)

    readyz_ok, checks = ready
    not_ready = [n["name"] for n in node_views if not n["ready"]]
    problem_pods = [f"{p['namespace']}/{p['name']} ({p['status']})" for p in pod_views if p["problem"]]
    alerts: list[Obj] = []
    if not readyz_ok:
        failing = [c["name"] for c in checks if not c["ok"]]
        alerts.append(
            {
                "severity": "critical",
                "message": f"API server not ready: {', '.join(failing) or 'readyz failed'}",
            }
        )
    for name in not_ready:
        alerts.append({"severity": "critical", "message": f"Node {name} is NotReady"})
    for node in node_views:
        for problem in node["problems"]:
            alerts.append({"severity": "warning", "message": f"Node {node['name']}: {problem}"})
    if problem_pods:
        alerts.append(
            {
                "severity": "warning",
                "message": f"{len(problem_pods)} pod(s) failing: {', '.join(problem_pods[:5])}",
                "pods": problem_pods[:20],
            }
        )
    return {
        "version": version.get("gitVersion"),
        "platform": version.get("platform"),
        "api_ready": readyz_ok,
        "nodes": {
            "total": len(node_views),
            "ready": len(node_views) - len(not_ready),
            "not_ready": not_ready,
        },
        "pods": {
            "total": len(pod_views),
            "running": phases.get("Running", 0),
            "pending": phases.get("Pending", 0),
            "failed": phases.get("Failed", 0),
            "succeeded": phases.get("Succeeded", 0),
            "problems": len(problem_pods),
            "restarts": sum(p["restarts"] for p in pod_views),
        },
        "cpu": {
            "allocatable_m": total("cpu", "allocatable_m"),
            "requested_m": total("cpu", "requested_m"),
            "used_m": total("cpu", "used_m") if metrics else None,
        },
        "memory": {
            "allocatable_bytes": total("memory", "allocatable_bytes"),
            "requested_bytes": total("memory", "requested_bytes"),
            "used_bytes": total("memory", "used_bytes") if metrics else None,
        },
        "metrics_available": metrics is not None,
        "alerts": alerts,
    }


def health_state(summary: Obj) -> str:
    """Map a summary to a cluster lifecycle state."""
    if not summary["api_ready"] or summary["nodes"]["ready"] == 0:
        return "degraded"
    if summary["nodes"]["not_ready"] or any(a["severity"] == "critical" for a in summary["alerts"]):
        return "degraded"
    return "active"


def detect_distribution(version: Obj, nodes: list[Obj], has_kubeadm_config: bool) -> DistributionType:
    git = str(version.get("gitVersion") or "")
    labels = [(_meta(n).get("labels") or {}) for n in nodes]
    provider_ids = [str((n.get("spec") or {}).get("providerID") or "") for n in nodes]
    names = [str(_meta(n).get("name") or "") for n in nodes]
    if "-eks-" in git:
        return DistributionType.EKS
    if "-gke." in git:
        return DistributionType.GKE
    if any(any(k.startswith("kubernetes.azure.com") for k in lbl) for lbl in labels):
        return DistributionType.AKS
    if "+k3s" in git:
        return DistributionType.K3D if any(n.startswith("k3d-") for n in names) else DistributionType.K3S
    if "+rke2" in git:
        return DistributionType.RKE2
    if any(p.startswith("kind://") for p in provider_ids):
        return DistributionType.KIND
    if has_kubeadm_config:
        return DistributionType.KUBEADM
    return DistributionType.OTHER
