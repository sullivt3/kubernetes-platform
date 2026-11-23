"""Namespace boundaries, non-adoption and atomic writes for managed manifests."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import re

from app.core.errors import InvalidStateError
from app.modules.kubernetes import client as kc
from app.modules.kubernetes.apply import Discovery, comparable


class ApplyConflict(InvalidStateError):
    """A foreign field manager must release ownership before syncing."""


OWNER = "kplatform.io/gitops-application"
REVIEW = "kplatform.io/gitops-review"


def fingerprint(obj):
    return hashlib.sha256(comparable(obj).encode()).hexdigest()


def normalized(api, objects, application_id, namespace):
    discovery = Discovery(api)
    result = []
    paths = set()
    for original in objects:
        obj = copy.deepcopy(original)
        meta = obj.get("metadata")
        if (
            not isinstance(meta, dict)
            or not isinstance(meta.get("name"), str)
            or not re.fullmatch(r"[a-z0-9]([-a-z0-9.]*[a-z0-9])?", meta["name"])
            or len(meta["name"]) > 253
            or not isinstance(obj.get("apiVersion"), str)
            or not re.fullmatch(r"[a-zA-Z0-9./-]+", obj["apiVersion"])
            or not isinstance(obj.get("kind"), str)
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", obj["kind"])
        ):
            raise InvalidStateError("Every manifest needs a valid API version, kind and explicit resource name")
        if not discovery.is_namespaced(obj):
            raise InvalidStateError("GitOps applications can manage only namespaced resources")
        if meta.get("namespace", namespace) != namespace:
            raise InvalidStateError("Manifest namespace differs from the application destination")
        if any(
            k in meta
            for k in ("uid", "resourceVersion", "managedFields", "ownerReferences", "generateName", "deletionTimestamp")
        ):
            raise InvalidStateError("Manifest metadata contains unsupported server or owner fields")
        if "status" in obj:
            raise InvalidStateError("Remove status fields from GitOps manifests")
        meta["namespace"] = namespace
        annotations = meta.setdefault("annotations", {})
        if not isinstance(annotations, dict) or any(not isinstance(v, str) for v in annotations.values()):
            raise InvalidStateError("Annotations must contain string values")
        if any(k in annotations for k in (OWNER, REVIEW)):
            raise InvalidStateError("GitOps ownership annotations are reserved")
        annotations[OWNER] = str(application_id)
        # Normalize write-only Secret stringData before comparison and encrypted persistence.
        if obj["apiVersion"] == "v1" and obj["kind"] == "Secret" and "stringData" in obj:
            strings = obj.pop("stringData")
            if not isinstance(strings, dict) or any(not isinstance(v, str) for v in strings.values()):
                raise InvalidStateError("Secret stringData must contain strings")
            data = obj.setdefault("data", {})
            if not isinstance(data, dict):
                raise InvalidStateError("Secret data must be a mapping")
            data.update({k: base64.b64encode(v.encode()).decode() for k, v in strings.items()})
        path = discovery.object_path(obj, namespace)
        if path in paths:
            raise InvalidStateError("Multiple manifests target the same Kubernetes resource")
        paths.add(path)
        result.append((path, obj))
    return result


def live_object(api, path):
    try:
        return kc.get_json(api, path)
    except kc.NotFoundInClusterError:
        return None


def owned(live, application_id):
    if live is not None and (live.get("metadata", {}).get("annotations") or {}).get(OWNER) != str(application_id):
        raise InvalidStateError("A target resource already exists outside this application; adoption is refused")
    if live and live["metadata"].get("deletionTimestamp"):
        raise InvalidStateError("A target resource is being deleted")


def patch(api, path, obj, manager, *, dry_run):
    params = {"fieldManager": manager, "force": "false"}
    if dry_run:
        params["dryRun"] = "All"
    status, data = api.request(
        "PATCH", path, params=params, body=json.dumps(obj).encode(), content_type="application/apply-patch+yaml"
    )
    if status == 409:
        raise ApplyConflict("Kubernetes reported field ownership conflicts; response withheld")
    if status >= 400:
        raise InvalidStateError(
            "Kubernetes rejected the manifest or reported field ownership conflicts; response withheld"
        )
    return json.loads(data)


def review_objects(api, objects, application_id, namespace):
    kc.get_json(api, f"/api/v1/namespaces/{namespace}")
    reviewed = []
    summary = []
    for path, obj in normalized(api, objects, application_id, namespace):
        live = live_object(api, path)
        owned(live, application_id)
        desired = copy.deepcopy(obj)
        if live:
            annotations = live["metadata"].get("annotations") or {}
            if REVIEW in annotations:
                desired["metadata"]["annotations"][REVIEW] = annotations[REVIEW]
        try:
            planned = patch(api, path, desired, "gitops-" + str(application_id), dry_run=True)
            action = (
                "create" if live is None else ("unchanged" if fingerprint(live) == fingerprint(planned) else "update")
            )
        except ApplyConflict:
            action = "conflict"
        reviewed.append(
            {
                "path": path,
                "object": obj,
                "uid": live["metadata"]["uid"] if live else None,
                "fingerprint": fingerprint(live),
            }
        )
        summary.append({"kind": obj["kind"], "name": obj["metadata"]["name"], "namespace": namespace, "action": action})
    return reviewed, summary


def finish_bootstrap(api, path, obj, live, manager):
    """Transfer only our POST manager after non-forced apply establishes shared ownership."""
    fields = live["metadata"].get("managedFields", [])
    if not any(m.get("manager") == manager and m.get("operation") == "Update" for m in fields):
        return live
    desired = copy.deepcopy(obj)
    desired["metadata"]["resourceVersion"] = live["metadata"]["resourceVersion"]
    shared = patch(api, path, desired, manager, dry_run=False)
    fields = shared["metadata"].get("managedFields", [])
    if not any(m.get("manager") == manager and m.get("operation") == "Apply" for m in fields):
        raise InvalidStateError("Kubernetes did not establish apply ownership; inspect the sync before retrying")
    indexes = [i for i, m in enumerate(fields) if m.get("manager") == manager and m.get("operation") == "Update"]
    operations = [
        {"op": "test", "path": "/metadata/uid", "value": shared["metadata"]["uid"]},
        {"op": "test", "path": "/metadata/resourceVersion", "value": shared["metadata"]["resourceVersion"]},
    ]
    for index in sorted(indexes, reverse=True):
        operations.extend(
            [
                {"op": "test", "path": f"/metadata/managedFields/{index}", "value": fields[index]},
                {"op": "remove", "path": f"/metadata/managedFields/{index}"},
            ]
        )
    status, data = api.request(
        "PATCH",
        path,
        params={"fieldManager": manager},
        body=json.dumps(operations).encode(),
        content_type="application/json-patch+json",
    )
    if status >= 400:
        raise InvalidStateError("Resource changed during bootstrap ownership handoff; review the sync before retrying")
    return json.loads(data)


def apply_reviewed(api, row, application_id, review_id):
    path = row["path"]
    obj = copy.deepcopy(row["object"])
    live = live_object(api, path)
    owned(live, application_id)
    annotations = (live or {}).get("metadata", {}).get("annotations") or {}
    if live and annotations.get(REVIEW) == str(review_id):
        desired = copy.deepcopy(obj)
        desired["metadata"]["annotations"][REVIEW] = str(review_id)
        planned = patch(api, path, desired, "gitops-" + str(application_id), dry_run=True)
        if fingerprint(live) == fingerprint(planned):
            return finish_bootstrap(api, path, desired, live, "gitops-" + str(application_id))
        raise InvalidStateError("A resource changed after this sync applied it; review again")
    if (live["metadata"]["uid"] if live else None) != row["uid"] or fingerprint(live) != row["fingerprint"]:
        raise InvalidStateError("A resource changed after review; review again")
    obj["metadata"]["annotations"][REVIEW] = str(review_id)
    if live:
        obj["metadata"]["resourceVersion"] = live["metadata"]["resourceVersion"]
        return patch(api, path, obj, "gitops-" + str(application_id), dry_run=False)
    # POST refuses a concurrent resource creation instead of adopting it through apply.
    status, data = api.request(
        "POST",
        path.rsplit("/", 1)[0],
        params={"fieldManager": "gitops-" + str(application_id)},
        body=json.dumps(obj).encode(),
        content_type="application/json",
    )
    if status >= 400:
        raise InvalidStateError("Kubernetes refused resource creation; review again. Response withheld.")
    return finish_bootstrap(api, path, obj, json.loads(data), "gitops-" + str(application_id))
