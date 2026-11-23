"""Reviewed, UID-bound pruning after desired workloads are ready; data resources retained."""

from __future__ import annotations

import json
import re
import time

from app.core.errors import InvalidStateError
from app.modules.gitops import resources
from app.modules.kubernetes.apply import Discovery

WAIT_SECONDS = 120
RETAIN = "kplatform.io/gitops-prune"
# API versions/groups matter: a custom kind called Deployment must not inherit deletion permission.
ELIGIBLE = {
    ("v1", "ConfigMap"),
    ("v1", "Secret"),
    ("v1", "Service"),
    ("v1", "ServiceAccount"),
    ("v1", "Pod"),
    ("v1", "ResourceQuota"),
    ("v1", "LimitRange"),
    ("apps/v1", "Deployment"),
    ("apps/v1", "ReplicaSet"),
    ("apps/v1", "DaemonSet"),
    ("batch/v1", "Job"),
    ("batch/v1", "CronJob"),
    ("networking.k8s.io/v1", "Ingress"),
    ("networking.k8s.io/v1", "NetworkPolicy"),
    ("rbac.authorization.k8s.io/v1", "Role"),
    ("rbac.authorization.k8s.io/v1", "RoleBinding"),
    ("autoscaling/v2", "HorizontalPodAutoscaler"),
}


def protected(live):
    annotations = live.get("metadata", {}).get("annotations") or {}
    return (
        (live.get("apiVersion"), live.get("kind")) not in ELIGIBLE
        or annotations.get(RETAIN, "").lower() in {"disabled", "false", "keep"}
        or annotations.get("helm.sh/resource-policy") == "keep"
    )


def candidates(api, inventory, desired_paths, application_id, namespace):
    plans = []
    summary = []
    retained = []
    discovery = Discovery(api)
    for path, item in inventory.items():
        if path in desired_paths:
            continue
        detail = {k: item[k] for k in ("kind", "name", "namespace")}
        if item["namespace"] != namespace or not re.fullmatch(
            r"/(?:api/v1|apis/[a-zA-Z0-9.-]+/[a-zA-Z0-9]+)"
            + re.escape("/namespaces/" + namespace + "/")
            + r"[a-z0-9-]+/[a-z0-9.-]+",
            path,
        ):
            raise InvalidStateError("Prune inventory is outside the application namespace")
        version = "v1" if path.startswith("/api/v1/") else "/".join(path.split("/")[2:4])
        discovery.resource(version, item["kind"])  # An unavailable API is not proof of object absence.
        live = resources.live_object(api, path)
        action = "delete"
        reason = "removed_from_git"
        if live is None:
            action = "absent"
            reason = "already_absent"
        elif (
            live["metadata"].get("uid") != item["uid"]
            or live["metadata"].get("namespace") != namespace
            or live["metadata"].get("name") != item["name"]
            or live.get("kind") != item["kind"]
            or discovery.object_path(live, namespace) != path
            or (live["metadata"].get("annotations") or {}).get(resources.OWNER) != str(application_id)
        ):
            action = "blocked"
            reason = "ownership_or_identity_changed"
        elif protected(live):
            action = "retain"
            reason = "protected_kind_or_retention_annotation"
        if action == "retain":
            retained.append({**detail, "reason": reason})
            continue
        plans.append({"path": path, "uid": item["uid"], "fingerprint": resources.fingerprint(live), **detail})
        summary.append({**detail, "action": action, "reason": reason})
    return plans, summary, retained


def check_identity(api, item, application_id, *, check_state=True):
    live = resources.live_object(api, item["path"])
    if live is None:
        return None
    meta = live["metadata"]
    if meta.get("uid") != item["uid"] or (meta.get("annotations") or {}).get(resources.OWNER) != str(application_id):
        raise InvalidStateError("Prune target ownership or UID changed; refusing deletion")
    if protected(live):
        raise InvalidStateError("Prune target became protected; refusing deletion")
    if check_state and not meta.get("deletionTimestamp") and resources.fingerprint(live) != item["fingerprint"]:
        raise InvalidStateError("Prune target changed after review; review again")
    return live


def ready(obj):
    kind = obj["kind"]
    status = obj.get("status") or {}
    spec = obj.get("spec") or {}
    if kind == "Pod":
        return status.get("phase") == "Succeeded" or any(
            c.get("type") == "Ready" and c.get("status") == "True" for c in status.get("conditions", [])
        )
    if kind == "Job":
        return status.get("succeeded", 0) >= (spec.get("completions") or 1)
    if kind not in {"Deployment", "StatefulSet", "ReplicaSet", "DaemonSet"}:
        return True
    if status.get("observedGeneration", 0) < obj["metadata"].get("generation", 1):
        return False
    if kind == "DaemonSet":
        desired = status.get("desiredNumberScheduled", 0)
        return status.get("numberReady", 0) >= desired and status.get("updatedNumberScheduled", 0) >= desired
    desired = spec.get("replicas", 1)
    if kind == "Deployment" and status.get("availableReplicas", 0) < desired:
        return False
    return status.get("readyReplicas", 0) >= desired and (
        kind == "ReplicaSet" or status.get("updatedReplicas", 0) >= desired
    )


def wait_ready(api, objects, ctx, guard):
    deadline = time.monotonic() + WAIT_SECONDS
    while True:
        ctx.check_cancelled()
        guard()
        waiting = False
        for item in objects:
            obj = resources.live_object(api, item["path"])
            if obj is None or not ready(obj):
                waiting = True
        if not waiting:
            return
        if time.monotonic() >= deadline:
            raise InvalidStateError("Pruning blocked: desired workloads are not ready")
        time.sleep(2)


def delete_owned(api, item, application_id, ctx, guard):
    ctx.check_cancelled()
    guard()
    live = check_identity(api, item, application_id)
    if live is None:
        return "absent"
    if not live["metadata"].get("deletionTimestamp"):
        status, _data = api.request(
            "DELETE",
            item["path"],
            body=json.dumps(
                {
                    "apiVersion": "v1",
                    "kind": "DeleteOptions",
                    "propagationPolicy": "Foreground",
                    "preconditions": {"uid": item["uid"], "resourceVersion": live["metadata"]["resourceVersion"]},
                }
            ).encode(),
            content_type="application/json",
        )
        if status not in {200, 202, 404}:
            raise InvalidStateError("Kubernetes refused guarded pruning; response withheld")
    deadline = time.monotonic() + WAIT_SECONDS
    while True:
        ctx.check_cancelled()
        guard()
        current = check_identity(api, item, application_id, check_state=False)
        if current is None:
            return "deleted"
        if time.monotonic() >= deadline:
            raise InvalidStateError("Prune deletion is still pending; finalizers and inventory were retained")
        time.sleep(2)
