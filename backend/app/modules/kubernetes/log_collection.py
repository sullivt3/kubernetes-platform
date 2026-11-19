"""Reconcile namespace-scoped Alloy collection through tracked operations."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, cast
from urllib.parse import quote

from pydantic import Field

from app.core.errors import ConflictError
from app.modules.kubernetes import client as kc
from app.modules.kubernetes.phase6 import workflow_api
from app.modules.kubernetes.service_proxy import DNSLabel
from app.modules.kubernetes.workflows import ConnectParams
from app.orchestration.context import StepContext
from app.orchestration.workflow import (
    OperationTarget,
    StepDefinition,
    StepFailedError,
    WorkflowDefinition,
    workflow_registry,
)

OWNER = "kubernetes-platform.io/log-collector"
CONFIG = "kplatform-log-collection"
ROLE = "kplatform-log-collection"


class CollectionParams(ConnectParams):
    collector_namespace: DNSLabel
    namespaces: list[DNSLabel] = Field(max_length=30)


def path(namespace: str, plural: str, name: str) -> str:
    base = "/apis/rbac.authorization.k8s.io/v1" if plural in {"roles", "rolebindings"} else "/api/v1"
    return f"{base}/namespaces/{namespace}/{plural}/{quote(name, safe='')}"


def optional(api: kc.ClusterAPI, url: str) -> dict | None:
    try:
        return kc.get_json(api, url)
    except kc.NotFoundInClusterError:
        return None


def owned(obj: dict, owner: str) -> bool:
    return (obj["metadata"].get("annotations") or {}).get(OWNER) == owner


def put_owned(api: kc.ClusterAPI, url: str, obj: dict, owner: str) -> None:
    existing = optional(api, url)
    if existing:
        if not owned(existing, owner):
            raise ConflictError("The log collection resource name is owned by another installation")
        obj["metadata"]["resourceVersion"] = existing["metadata"]["resourceVersion"]
        kc.send_json(api, "PUT", url, obj)
    else:
        kc.send_json(api, "POST", url.rsplit("/", 1)[0], obj)


def alloy_config(namespaces: list[str]) -> str:
    return """
discovery.kubernetes "pods" {
  role = "pod"
  namespaces {
    names = NAMESPACES
  }
}
discovery.relabel "pods" {
  targets = discovery.kubernetes.pods.targets
  rule {
    source_labels = ["__meta_kubernetes_pod_label_app_kubernetes_io_name"]
    regex = "alloy|loki"
    action = "drop"
  }
  rule {
    source_labels = ["__meta_kubernetes_namespace"]
    target_label = "namespace"
  }
  rule {
    source_labels = ["__meta_kubernetes_pod_name"]
    target_label = "pod"
  }
  rule {
    source_labels = ["__meta_kubernetes_pod_container_name"]
    target_label = "container"
  }
}
loki.source.kubernetes "pods" {
  targets = discovery.relabel.pods.output
  forward_to = [loki.write.local.receiver]
}
loki.write "local" {
  endpoint {
    url = "http://loki:3100/loki/api/v1/push"
  }
}
""".replace("NAMESPACES", json.dumps(namespaces))


def reconcile(ctx: StepContext) -> dict[str, Any]:
    p: CollectionParams = ctx.params  # type: ignore[assignment]
    api = workflow_api(ctx, p.cluster_id)
    owner, ns = str(p.cluster_id), p.collector_namespace
    deploy_path = f"/apis/apps/v1/namespaces/{ns}/deployments/alloy"
    deploy = kc.get_json(api, deploy_path)
    if not owned(deploy, owner):
        raise StepFailedError("Register this Alloy installation for managed collection before changing namespaces")
    if not p.namespaces:
        raise StepFailedError("Select at least one namespace")
    desired = sorted(set(p.namespaces))
    sa = deploy["spec"]["template"]["spec"].get("serviceAccountName")
    if sa != "alloy":
        raise StepFailedError("The managed collector must use the alloy ServiceAccount")
    for target in desired:
        ctx.check_cancelled()
        kc.get_json(api, f"/api/v1/namespaces/{target}")
        for kind, plural, spec in [
            (
                "Role",
                "roles",
                {
                    "rules": [
                        {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list", "watch"]},
                        {"apiGroups": [""], "resources": ["pods/log"], "verbs": ["get"]},
                    ]
                },
            ),
            (
                "RoleBinding",
                "rolebindings",
                {
                    "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": ROLE},
                    "subjects": [{"kind": "ServiceAccount", "name": "alloy", "namespace": ns}],
                },
            ),
        ]:
            obj: dict[str, Any] = {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": kind,
                "metadata": {"name": ROLE, "namespace": target, "annotations": {OWNER: owner}},
                **cast(dict[str, Any], spec),
            }
            put_owned(api, path(target, plural, ROLE), obj, owner)
    configuration = alloy_config(desired)
    digest = hashlib.sha256(configuration.encode()).hexdigest()[:12]
    config_name = f"{CONFIG}-{digest}"
    obj = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": config_name,
            "namespace": ns,
            "annotations": {OWNER: owner, "kubernetes-platform.io/collected-namespaces": json.dumps(desired)},
        },
        "data": {"config.alloy": configuration},
    }
    put_owned(api, path(ns, "configmaps", config_name), obj, owner)
    patch = {"spec": {"template": {"spec": {"volumes": [{"name": "config", "configMap": {"name": config_name}}]}}}}
    kc.send_json(api, "PATCH", deploy_path, patch, content_type="application/strategic-merge-patch+json")
    for _ in range(90):
        ctx.check_cancelled()
        current = kc.get_json(api, deploy_path)
        status = current.get("status") or {}
        if (
            status.get("observedGeneration", 0) >= current["metadata"]["generation"]
            and status.get("updatedReplicas") == 1
            and status.get("availableReplicas") == 1
            and status.get("replicas") == 1
        ):
            break
        time.sleep(2)
    else:
        raise StepFailedError("Collector rollout is incomplete. Review pod events and retry the operation.")
    # Revoke only grants created by this manager after the new collector is ready.
    for plural in ("roles", "rolebindings"):
        rows, truncated = kc.list_items(api, f"/apis/rbac.authorization.k8s.io/v1/{plural}")
        if truncated:
            raise StepFailedError("Grant inventory was truncated. Retry with sufficient collection visibility.")
        for resource in rows:
            meta = resource["metadata"]
            if meta["name"] == ROLE and owned(resource, owner) and meta["namespace"] not in desired:
                kc.send_json(
                    api,
                    "DELETE",
                    path(meta["namespace"], plural, ROLE),
                    {"kind": "DeleteOptions", "preconditions": {"uid": meta["uid"]}},
                )
    result = {"namespaces": desired}
    ctx.set_result(result)
    return result


workflow_registry.register(
    WorkflowDefinition(
        type="cluster.logs.collection",
        title="Update log collection",
        description="Reconcile Alloy namespace access and configuration.",
        params_model=CollectionParams,
        plan=lambda _: [StepDefinition("configure", "Configure and verify Alloy", reconcile)],
        target=lambda p: OperationTarget("cluster", str(p.cluster_id), p.cluster_name),
    )
)
