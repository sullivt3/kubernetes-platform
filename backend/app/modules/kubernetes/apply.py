"""Apply manifests through the Kubernetes API using discovery + server-side apply.

Works for any resource the cluster serves (including CRDs) without a hard-coded
kind -> path table, and is idempotent (re-applying converges to the same state).

``plan_objects`` runs a server-side **dry run** (the API server performs full
validation, defaulting and admission without persisting anything) and returns a
per-object diff against the live object — an exact preview of what apply will do.
"""

from __future__ import annotations

import copy
import difflib
from typing import Any

import yaml

from app.core.errors import PlatformError, ValidationFailedError
from app.modules.kubernetes.client import (
    ClusterAPI,
    KubernetesAPIError,
    NotFoundInClusterError,
    get_json,
)

Obj = dict[str, Any]
FIELD_MANAGER = "kubernetes-platform"
VOLATILE_META = ("managedFields", "uid", "resourceVersion", "generation", "creationTimestamp", "selfLink")


class Discovery:
    def __init__(self, api: ClusterAPI) -> None:
        self.api = api
        self._cache: dict[str, dict[str, tuple[str, bool]]] = {}

    def resource(self, api_version: str, kind: str) -> tuple[str, bool]:
        """(plural resource name, namespaced) for an apiVersion/kind."""
        if api_version not in self._cache:
            path = "/api/v1" if api_version == "v1" else f"/apis/{api_version}"
            try:
                listing = get_json(self.api, path)
            except NotFoundInClusterError:
                raise ValidationFailedError(f"The cluster does not serve API version {api_version}") from None
            self._cache[api_version] = {
                r["kind"]: (r["name"], bool(r.get("namespaced")))
                for r in listing.get("resources", [])
                if "/" not in r["name"]  # skip subresources
            }
        try:
            return self._cache[api_version][kind]
        except KeyError:
            raise ValidationFailedError(f"The cluster does not serve {kind} in {api_version}") from None

    def object_path(self, obj: Obj, default_namespace: str = "default") -> str:
        api_version, kind = obj.get("apiVersion"), obj.get("kind")
        name = (obj.get("metadata") or {}).get("name")
        if not (api_version and kind and name):
            raise ValidationFailedError("Every manifest object needs apiVersion, kind and metadata.name")
        plural, namespaced = self.resource(api_version, kind)
        base = "/api/v1" if api_version == "v1" else f"/apis/{api_version}"
        if namespaced:
            namespace = obj["metadata"].get("namespace") or default_namespace
            return f"{base}/namespaces/{namespace}/{plural}/{name}"
        return f"{base}/{plural}/{name}"

    def is_namespaced(self, obj: Obj) -> bool:
        return self.resource(str(obj.get("apiVersion")), str(obj.get("kind")))[1]


def parse_manifest(text: str) -> list[Obj]:
    try:
        docs = [d for d in yaml.safe_load_all(text) if d]
    except yaml.YAMLError as exc:
        raise ValidationFailedError(f"Manifest is not valid YAML: {exc}") from None
    objects: list[Obj] = []
    for doc in docs:
        if not isinstance(doc, dict):
            raise ValidationFailedError("Manifest documents must be objects")
        if str(doc.get("kind", "")).endswith("List") and isinstance(doc.get("items"), list):
            objects.extend(doc["items"])
        else:
            objects.append(doc)
    if not objects:
        raise ValidationFailedError("The manifest contains no objects")
    return objects


def _apply(api: ClusterAPI, path: str, obj: Obj, *, dry_run: bool) -> Obj:
    import json

    params: dict[str, Any] = {"fieldManager": FIELD_MANAGER, "force": "true"}
    if dry_run:
        params["dryRun"] = "All"
    status, data = api.request(
        "PATCH", path, params=params, body=json.dumps(obj).encode(), content_type="application/apply-patch+yaml"
    )
    if status >= 400:
        from app.modules.kubernetes.client import _raise_for

        _raise_for(status, data, path)
    return json.loads(data)


def comparable(obj: Obj | None) -> str:
    """YAML of an object without server-managed noise, for diffs."""
    if obj is None:
        return ""
    result = copy.deepcopy(obj)
    result.pop("status", None)
    meta = result.get("metadata") or {}
    for field in VOLATILE_META:
        meta.pop(field, None)
    (meta.get("annotations") or {}).pop("kubectl.kubernetes.io/last-applied-configuration", None)
    if meta.get("annotations") == {}:
        meta.pop("annotations")
    return yaml.safe_dump(result, sort_keys=True)


def _ordered(objects: list[Obj]) -> list[Obj]:
    priority = {"Namespace": 0, "CustomResourceDefinition": 1}
    return sorted(objects, key=lambda o: priority.get(str(o.get("kind", "")), 2))


def _label(obj: Obj, namespace: str | None) -> str:
    meta = obj.get("metadata") or {}
    return f"{obj.get('kind')}/{(namespace + '/') if namespace else ''}{meta.get('name')}"


def plan_objects(
    api: ClusterAPI, objects: list[Obj], *, default_namespace: str = "default", apply: bool = False
) -> list[Obj]:
    """Dry-run (and optionally apply) each object; returns per-object results with diffs and errors."""
    discovery = Discovery(api)
    results: list[Obj] = []
    new_namespaces: set[str] = set()  # created by this manifest, not yet persisted during a dry run
    for obj in _ordered(objects):
        entry: Obj = {"kind": obj.get("kind"), "name": (obj.get("metadata") or {}).get("name"), "namespace": None}
        try:
            path = discovery.object_path(obj, default_namespace)
            namespace = (
                ((obj.get("metadata") or {}).get("namespace") or default_namespace)
                if discovery.is_namespaced(obj)
                else None
            )
            entry["namespace"] = namespace
            if not apply and namespace in new_namespaces:
                # A server dry run cannot see a namespace that only exists in this manifest.
                entry.update(
                    action="create",
                    applied=False,
                    note=f"Namespace '{namespace}' is created by this manifest; server-side validation runs on apply.",
                    diff="".join(
                        difflib.unified_diff(
                            [], comparable(obj).splitlines(keepends=True), "live", f"applied/{_label(obj, namespace)}"
                        )
                    ),
                )
                results.append(entry)
                continue
            try:
                live: Obj | None = get_json(api, path)
            except NotFoundInClusterError:
                live = None
            planned = _apply(api, path, obj, dry_run=True)
            if obj.get("kind") == "Namespace" and obj.get("apiVersion") == "v1" and live is None:
                new_namespaces.add(str((obj.get("metadata") or {}).get("name")))
            before, after = comparable(live), comparable(planned)
            entry["action"] = "create" if live is None else ("unchanged" if before == after else "update")
            entry["diff"] = "".join(
                difflib.unified_diff(
                    before.splitlines(keepends=True),
                    after.splitlines(keepends=True),
                    fromfile=f"live/{_label(obj, namespace)}",
                    tofile=f"applied/{_label(obj, namespace)}",
                )
            )
            if apply and entry["action"] != "unchanged":
                _apply(api, path, obj, dry_run=False)
                entry["applied"] = True
            else:
                entry["applied"] = False
        except KubernetesAPIError as exc:
            entry.update(action="error", error=exc.message, causes=(exc.details or {}).get("causes", []), applied=False)
        except PlatformError as exc:
            entry.update(action="error", error=exc.message, causes=[], applied=False)
        results.append(entry)
        if apply and entry["action"] == "error":
            break  # stop at the first failure; later objects may depend on it
    return results


def apply_objects(api: ClusterAPI, objects: list[Obj], *, on_applied: Any = None) -> list[str]:
    """Apply objects in order (namespaces and CRDs first). Returns 'Kind/name' for each."""
    discovery = Discovery(api)
    applied = []
    for obj in _ordered(objects):
        _apply(api, discovery.object_path(obj), obj, dry_run=False)
        label = f"{obj['kind']}/{obj['metadata']['name']}"
        applied.append(label)
        if on_applied:
            on_applied(label)
    return applied
