"""Live reads from a cluster's Kubernetes API."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

from app.modules.kubernetes import views
from app.modules.kubernetes.client import (
    ClusterAPI,
    ClusterAuthError,
    ClusterUnreachableError,
    KubernetesAPIError,
    NotFoundInClusterError,
    get_json,
    list_items,
)

Obj = dict[str, Any]


def version(api: ClusterAPI) -> Obj:
    return get_json(api, "/version", timeout=10)


def readyz(api: ClusterAPI) -> tuple[bool, list[Obj]]:
    status, data = api.request("GET", "/readyz", params={"verbose": "1"}, timeout=10)
    checks = []
    for line in data.decode(errors="replace").splitlines():
        if line.startswith("[+]") or line.startswith("[-]"):
            name = line[3:].split(" ", 1)[0]
            checks.append({"name": name, "ok": line.startswith("[+]")})
    return status == 200, checks


def node_metrics(api: ClusterAPI) -> Obj | None:
    """metrics.k8s.io node usage, or None when metrics-server is not installed/available."""
    try:
        return get_json(api, "/apis/metrics.k8s.io/v1beta1/nodes", timeout=10)
    except NotFoundInClusterError:
        return None
    except KubernetesAPIError as exc:
        if exc.http_status in (503, 500):
            return None
        raise


def has_kubeadm_config(api: ClusterAPI) -> bool:
    try:
        get_json(api, "/api/v1/namespaces/kube-system/configmaps/kubeadm-config", timeout=10)
        return True
    except (NotFoundInClusterError, ClusterAuthError):
        return False


def nodes(api: ClusterAPI) -> list[Obj]:
    return list_items(api, "/api/v1/nodes")[0]


def pods(api: ClusterAPI, namespace: str | None = None, *, node: str | None = None) -> tuple[list[Obj], bool]:
    path = f"/api/v1/namespaces/{namespace}/pods" if namespace else "/api/v1/pods"
    params = {"fieldSelector": f"spec.nodeName={node}"} if node else None
    return list_items(api, path, params)


def namespaces(api: ClusterAPI) -> list[Obj]:
    return list_items(api, "/api/v1/namespaces")[0]


def events(
    api: ClusterAPI, namespace: str | None = None, field_selector: str | None = None, limit: int = 200
) -> list[Obj]:
    path = f"/api/v1/namespaces/{namespace}/events" if namespace else "/api/v1/events"
    items, _ = list_items(api, path, {"fieldSelector": field_selector} if field_selector else None, max_items=2000)
    viewed = [views.event_view(e) for e in items]
    viewed.sort(key=lambda e: e["last_seen"] or "", reverse=True)
    return viewed[:limit]


def summary(api: ClusterAPI) -> Obj:
    """Full health summary. Raises ClusterUnreachableError/ClusterAuthError when the API can't be used."""
    ver = version(api)  # fail fast if unreachable
    with ThreadPoolExecutor(max_workers=4) as pool:
        f_ready = pool.submit(readyz, api)
        f_nodes = pool.submit(nodes, api)
        f_pods = pool.submit(pods, api)
        f_metrics = pool.submit(node_metrics, api)
        ready, node_list, (pod_list, truncated), metrics = (
            f_ready.result(),
            f_nodes.result(),
            f_pods.result(),
            f_metrics.result(),
        )
    result = views.summarize(ver, ready, node_list, pod_list, metrics)
    result["readyz_checks"] = ready[1]
    result["pods_truncated"] = truncated
    return result


__all__ = [
    "ClusterAuthError",
    "ClusterUnreachableError",
    "events",
    "has_kubeadm_config",
    "namespaces",
    "node_metrics",
    "nodes",
    "pods",
    "readyz",
    "summary",
    "version",
]
