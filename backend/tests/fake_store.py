"""A generic in-memory Kubernetes API for workload tests (implements ClusterAPI).

Supports: discovery, GET item/collection (label selectors), server-side apply
(including dryRun), merge / strategic-merge / JSON patches, POST, DELETE, the
scale subresource, pod logs and a validation hook that returns 422.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Iterator
from typing import Any

from app.modules.kubernetes.client import ConnectionTarget

RESOURCES = {
    "v1": [
        ("namespaces", "Namespace", False),
        ("pods", "Pod", True),
        ("services", "Service", True),
        ("configmaps", "ConfigMap", True),
        ("secrets", "Secret", True),
        ("events", "Event", True),
    ],
    "apps/v1": [
        ("deployments", "Deployment", True),
        ("replicasets", "ReplicaSet", True),
        ("statefulsets", "StatefulSet", True),
        ("daemonsets", "DaemonSet", True),
    ],
    "batch/v1": [("jobs", "Job", True), ("cronjobs", "CronJob", True)],
}


def merge(base: Any, patch: Any) -> Any:
    if isinstance(base, dict) and isinstance(patch, dict):
        result = dict(base)
        for key, value in patch.items():
            if value is None:
                result.pop(key, None)
            else:
                result[key] = merge(base.get(key), value)
        return result
    return copy.deepcopy(patch)


class FakeStore:
    def __init__(self) -> None:
        self.objects: dict[str, dict[str, Any]] = {}
        self.requests: list[tuple[str, str, dict[str, Any]]] = []
        self.logs: dict[str, str] = {}
        self.uid = 0
        self.put_namespace("default")

    # ------------------------------------------------------------------ helpers

    def factory(self, target: ConnectionTarget) -> FakeStore:
        return self

    def close(self) -> None:
        return None

    @staticmethod
    def path_for(obj: dict[str, Any]) -> str:
        api_version, kind = obj["apiVersion"], obj["kind"]
        plural, namespaced = next((p, n) for p, k, n in RESOURCES[api_version] if k == kind)
        base = "/api/v1" if api_version == "v1" else f"/apis/{api_version}"
        meta = obj["metadata"]
        return (
            f"{base}/namespaces/{meta.get('namespace', 'default')}/{plural}/{meta['name']}"
            if namespaced
            else f"{base}/{plural}/{meta['name']}"
        )

    def put(self, obj: dict[str, Any]) -> dict[str, Any]:
        obj = copy.deepcopy(obj)
        meta = obj.setdefault("metadata", {})
        namespaced_kinds = {k for group in RESOURCES.values() for _, k, n in group if n}
        if obj["kind"] in namespaced_kinds:
            meta.setdefault("namespace", "default")
        self.uid += 1
        meta.setdefault("uid", f"uid-{self.uid}")
        meta.setdefault("creationTimestamp", "2026-01-01T00:00:00Z")
        meta["generation"] = meta.get("generation", 0) + 1
        self.objects[self.path_for(obj)] = obj
        return obj

    def put_namespace(self, name: str) -> None:
        self.put({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": name}})

    def _ok(self, obj: Any, status: int = 200) -> tuple[int, bytes]:
        return status, json.dumps(obj).encode()

    def _err(self, status: int, message: str, causes: list[dict[str, str]] | None = None) -> tuple[int, bytes]:
        body: dict[str, Any] = {
            "kind": "Status",
            "message": message,
            "reason": "Invalid" if status == 422 else "NotFound",
        }
        if causes:
            body["details"] = {"causes": causes}
        return status, json.dumps(body).encode()

    def validate(self, obj: dict[str, Any]) -> tuple[int, bytes] | None:
        replicas = (obj.get("spec") or {}).get("replicas")
        if isinstance(replicas, int) and replicas < 0:
            return self._err(
                422,
                f'{obj["kind"]}.apps "{obj["metadata"]["name"]}" is invalid: spec.replicas: Invalid value: {replicas}',
                [
                    {
                        "field": "spec.replicas",
                        "message": "must be greater than or equal to 0",
                        "reason": "FieldValueInvalid",
                    }
                ],
            )
        namespace = (obj.get("metadata") or {}).get("namespace")
        if namespace and obj["kind"] != "Namespace" and f"/api/v1/namespaces/{namespace}" not in self.objects:
            return self._err(404, f'namespaces "{namespace}" not found')
        return None

    # ------------------------------------------------------------------ ClusterAPI

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
        params = params or {}
        payload = json.loads(body) if isinstance(body, bytes) else body
        self.requests.append((method, path, params))
        if path in ("/api/v1", "/apis/apps/v1", "/apis/batch/v1"):
            group = "v1" if path == "/api/v1" else path[len("/apis/") :]
            return self._ok({"resources": [{"name": p, "kind": k, "namespaced": n} for p, k, n in RESOURCES[group]]})
        if path.endswith("/log"):
            return 200, self.logs.get(path, "").encode()
        if path.endswith("/scale"):
            target = path[: -len("/scale")]
            obj = self.objects.get(target)
            if obj is None:
                return self._err(404, "not found")
            if method == "PATCH":
                obj["spec"]["replicas"] = payload["spec"]["replicas"]
            return self._ok({"spec": {"replicas": obj["spec"].get("replicas", 1)}})
        if method == "GET":
            if path in self.objects:
                return self._ok(self.objects[path])
            plurals = {plural for group in RESOURCES.values() for plural, _, _ in group}
            namespaced = re.match(r"^(/api/v1|/apis/[^/]+/[^/]+)/namespaces/([^/]+)/([^/]+)$", path)
            cluster_wide = re.match(r"^(/api/v1|/apis/[^/]+/[^/]+)/([^/]+)$", path)
            if namespaced and namespaced.group(3) in plurals:
                items = [o for q, o in self.objects.items() if q.startswith(path + "/")]
            elif cluster_wide and cluster_wide.group(2) in plurals:
                base, plural = cluster_wide.group(1), cluster_wide.group(2)
                items = [
                    o for q, o in self.objects.items() if q.startswith(base + "/") and q.rsplit("/", 2)[-2] == plural
                ]
            else:
                return self._err(404, "not found")
            selector = params.get("labelSelector")
            if selector:
                wanted = dict(part.split("=", 1) for part in selector.split(","))
                items = [
                    o for o in items if all((o["metadata"].get("labels") or {}).get(k) == v for k, v in wanted.items())
                ]
            if str(params.get("fieldSelector", "")).startswith("involvedObject"):
                items = []
            return self._ok({"items": items, "metadata": {}})
        if method == "PATCH" and content_type == "application/apply-patch+yaml":
            error = self.validate(payload)
            if error:
                return error
            existing = self.objects.get(path)
            result = merge(existing or {}, payload)
            result.setdefault("metadata", {}).setdefault(
                "namespace",
                payload["metadata"].get("namespace")
                or (path.split("/")[path.split("/").index("namespaces") + 1] if "/namespaces/" in path else None),
            )
            if result["metadata"].get("namespace") is None:
                result["metadata"].pop("namespace")
            if params.get("dryRun") == "All":
                return self._ok(result)
            return self._ok(self.put(result))
        if method == "PATCH":
            obj = self.objects.get(path)
            if obj is None:
                return self._err(404, "not found")
            if content_type == "application/json-patch+json":
                for op in payload:
                    assert op["op"] == "replace" and op["path"] == "/spec/template"
                    obj["spec"]["template"] = op["value"]
            else:
                obj.update(merge(obj, payload))
            obj["metadata"]["generation"] = obj["metadata"].get("generation", 1) + 1
            return self._ok(obj)
        if method == "POST":
            payload.setdefault("metadata", {}).setdefault(
                "namespace", path.split("/")[path.split("/").index("namespaces") + 1]
            )
            return self._ok(self.put(payload), 201)
        if method == "DELETE":
            if path not in self.objects:
                return self._err(404, "not found")
            del self.objects[path]
            return self._ok({"status": "Success"})
        return self._err(404, "not found")

    def stream(self, path: str, params: dict[str, Any] | None = None) -> Iterator[str]:
        yield from self.logs.get(path, "").splitlines()
