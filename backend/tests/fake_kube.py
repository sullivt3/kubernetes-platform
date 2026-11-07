"""An in-memory Kubernetes API for tests (implements the ClusterAPI protocol)."""

from __future__ import annotations

import base64
import json
from typing import Any
from urllib.parse import parse_qs

from app.modules.kubernetes.client import ClusterUnreachableError, ConnectionTarget


def _node(name: str, role: str, *, ready: bool = True, cpu: str = "4", memory: str = "8Gi") -> dict[str, Any]:
    return {
        "metadata": {
            "name": name,
            "labels": {f"node-role.kubernetes.io/{role}": ""},
            "creationTimestamp": "2026-01-01T00:00:00Z",
        },
        "spec": {},
        "status": {
            "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
            "nodeInfo": {
                "kubeletVersion": "v1.37.2",
                "osImage": "Ubuntu 24.04",
                "containerRuntimeVersion": "containerd://2.4.2",
                "kernelVersion": "6.8",
                "architecture": "amd64",
            },
            "addresses": [{"type": "InternalIP", "address": f"10.0.0.{len(name)}"}],
            "capacity": {"cpu": cpu, "memory": memory, "pods": "110"},
            "allocatable": {"cpu": cpu, "memory": memory, "pods": "110"},
        },
    }


def _pod(
    name: str, namespace: str, node: str, *, waiting: str | None = None, cpu: str = "100m", restarts: int = 0
) -> dict[str, Any]:
    state = {"waiting": {"reason": waiting}} if waiting else {"running": {}}
    return {
        "metadata": {
            "name": name,
            "namespace": namespace,
            "creationTimestamp": "2026-01-01T00:00:00Z",
            "ownerReferences": [{"kind": "ReplicaSet", "name": f"{name}-rs"}],
        },
        "spec": {
            "nodeName": node,
            "containers": [{"name": "c", "image": "nginx", "resources": {"requests": {"cpu": cpu, "memory": "64Mi"}}}],
        },
        "status": {
            "phase": "Running",
            "podIP": "10.244.0.5",
            "containerStatuses": [{"name": "c", "ready": waiting is None, "restartCount": restarts, "state": state}],
        },
    }


class FakeKube:
    def __init__(self) -> None:
        self.unreachable = False
        self.version = {"gitVersion": "v1.37.2", "platform": "linux/amd64"}
        self.ready = True
        self.nodes = [_node("cp-1", "control-plane"), _node("worker-1", "worker")]
        self.namespaces = [
            {
                "metadata": {"name": n, "creationTimestamp": "2026-01-01T00:00:00Z"},
                "status": {"phase": "Active"},
            }
            for n in ("default", "kube-system", "apps")
        ]
        self.pods = [
            _pod("coredns-1", "kube-system", "cp-1"),
            _pod("web-1", "apps", "worker-1", cpu="250m"),
            _pod("web-2", "apps", "worker-1", waiting="CrashLoopBackOff", restarts=7),
        ]
        self.events = [
            {
                "metadata": {"namespace": "apps"},
                "type": "Warning",
                "reason": "BackOff",
                "message": "Back-off restarting",
                "involvedObject": {"kind": "Pod", "name": "web-2"},
                "count": 7,
                "lastTimestamp": "2026-01-01T00:05:00Z",
            }
        ]
        self.metrics: dict[str, Any] | None = None
        self.kubeadm = True
        self.applied: list[tuple[str, dict[str, Any]]] = []
        self.requests: list[tuple[str, str]] = []
        self.targets: list[ConnectionTarget] = []

    def factory(self, target: ConnectionTarget) -> FakeKube:
        self.targets.append(target)
        return self

    def close(self) -> None:
        return None

    def _ok(self, obj: Any) -> tuple[int, bytes]:
        return 200, json.dumps(obj).encode()

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
        if self.unreachable:
            raise ClusterUnreachableError("Cannot reach the Kubernetes API (simulated)")
        params = params or {}
        self.requests.append((method, path))
        if method == "PATCH":
            self.applied.append((path, json.loads(body)))
            if "/apis/apps/v1/namespaces/kube-system/deployments/metrics-server" in path:
                self.metrics = {
                    "items": [
                        {
                            "metadata": {"name": n["metadata"]["name"]},
                            "usage": {"cpu": "500m", "memory": "1Gi"},
                        }
                        for n in self.nodes
                    ]
                }
            return self._ok(json.loads(body))
        if path == "/version":
            return self._ok(self.version)
        if path == "/readyz":
            return (200 if self.ready else 500), (
                b"[+]ping ok\n[+]etcd ok\nreadyz check passed" if self.ready else b"[+]ping ok\n[-]etcd failed\n"
            )
        if path == "/api/v1/nodes":
            return self._ok({"items": self.nodes, "metadata": {}})
        if path.startswith("/api/v1/nodes/"):
            name = path.rsplit("/", 1)[1]
            node = next((n for n in self.nodes if n["metadata"]["name"] == name), None)
            return self._ok(node) if node else (404, b'{"message":"not found"}')
        if path in ("/api/v1/pods",) or (path.startswith("/api/v1/namespaces/") and path.endswith("/pods")):
            pods = self.pods
            if path != "/api/v1/pods":
                ns = path.split("/")[4]
                pods = [p for p in pods if p["metadata"]["namespace"] == ns]
            selector = params.get("fieldSelector", "")
            if selector.startswith("spec.nodeName="):
                pods = [p for p in pods if p["spec"]["nodeName"] == selector.split("=", 1)[1]]
            return self._ok({"items": pods, "metadata": {}})
        if path == "/api/v1/namespaces":
            return self._ok({"items": self.namespaces, "metadata": {}})
        if path.endswith("/events"):
            return self._ok({"items": self.events, "metadata": {}})
        if path == "/apis/metrics.k8s.io/v1beta1/nodes":
            return (
                self._ok(self.metrics)
                if self.metrics
                else (404, b'{"message":"the server could not find the requested resource"}')
            )
        if path == "/api/v1/namespaces/kube-system/configmaps/kubeadm-config":
            return self._ok({}) if self.kubeadm else (404, b"{}")
        if path == "/api/v1":
            return self._ok(
                {
                    "resources": [
                        {"name": n, "kind": k, "namespaced": ns}
                        for n, k, ns in (
                            ("serviceaccounts", "ServiceAccount", True),
                            ("services", "Service", True),
                            ("namespaces", "Namespace", False),
                        )
                    ]
                }
            )
        if path.startswith("/apis/"):
            group = path[len("/apis/") :]
            catalog = {
                "rbac.authorization.k8s.io/v1": [
                    ("clusterroles", "ClusterRole", False),
                    ("clusterrolebindings", "ClusterRoleBinding", False),
                    ("rolebindings", "RoleBinding", True),
                ],
                "apps/v1": [("deployments", "Deployment", True)],
                "apiregistration.k8s.io/v1": [("apiservices", "APIService", False)],
            }
            if group in catalog:
                return self._ok(
                    {"resources": [{"name": n, "kind": k, "namespaced": ns} for n, k, ns in catalog[group]]}
                )
        return 404, b'{"message":"not found"}'


def kubeconfig_text(*, server: str = "https://10.0.0.11:6443", auth: str = "cert", extra_cluster: str = "") -> str:
    ca = base64.b64encode(b"-----BEGIN CERTIFICATE-----\nCA\n-----END CERTIFICATE-----\n").decode()
    crt = base64.b64encode(b"-----BEGIN CERTIFICATE-----\nCLIENT\n-----END CERTIFICATE-----\n").decode()
    key = base64.b64encode(b"-----BEGIN PRIVATE KEY-----\nKEY\n-----END PRIVATE KEY-----\n").decode()
    user = {
        "cert": f"    client-certificate-data: {crt}\n    client-key-data: {key}\n",
        "token": "    token: sa-token-123456\n",
        "exec": "    exec:\n      apiVersion: client.authentication.k8s.io/v1beta1\n      command: aws\n",
    }[auth]
    return (
        "apiVersion: v1\nkind: Config\ncurrent-context: admin@prod\n"
        f"clusters:\n- name: prod\n  cluster:\n    server: {server}\n"
        f"    certificate-authority-data: {ca}\n{extra_cluster}"
        "- name: other\n  cluster:\n    server: https://other:6443\n    certificate-authority-data: " + ca + "\n"
        "contexts:\n- name: admin@prod\n  context: {cluster: prod, user: admin}\n"
        "- name: other\n  context: {cluster: other, user: other}\n"
        f"users:\n- name: admin\n  user:\n{user}- name: other\n  user:\n    token: other-token\n"
    )


def parse_query(path_with_query: str) -> dict[str, list[str]]:
    return parse_qs(path_with_query.split("?", 1)[1]) if "?" in path_with_query else {}
