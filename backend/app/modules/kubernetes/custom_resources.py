"""Discovery-driven CRD inventory and reviewed single-resource CRUD."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import select

from app.core.errors import (
    ConfirmationRequiredError,
    ConflictError,
    InvalidStateError,
    NotFoundError,
    PermissionDeniedError,
    ValidationFailedError,
)
from app.core.secrets import SealedSecret
from app.modules.auditing import service as audit
from app.modules.helm.catalog import values as validate_mapping
from app.modules.kubernetes import client as kc
from app.modules.kubernetes.apply import comparable
from app.modules.kubernetes.custom_resource_models import CustomResourceReview
from app.modules.organizations.policy import require

BASE = "/apis/apiextensions.k8s.io/v1/customresourcedefinitions"
NAME = r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$"
DNS = r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"
VERSION = r"^[a-z][a-z0-9]{0,62}$"
MARKER = "kplatform.io/custom-resource-review"
MANAGER = "kubernetes-platform-custom-resources"


class Target(BaseModel):
    definition: str = Field(pattern=NAME, max_length=253)
    version: str = Field(pattern=VERSION)
    definition_uid: str | None = Field(default=None, max_length=128)
    name: str = Field(pattern=NAME, max_length=253)
    namespace: str | None = Field(default=None, pattern=DNS, max_length=63)
    expected_uid: str | None = Field(default=None, max_length=128)


class ReviewInput(Target):
    definition_uid: str = Field(min_length=1, max_length=128)
    action: Literal["create", "update", "delete"]
    manifest: SecretStr = SecretStr("")
    expected_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class ExecuteInput(BaseModel):
    review_id: uuid.UUID
    confirm: str


def request(api, method, path, *, body=None, params=None):
    status, data = api.request(
        method, path, body=json.dumps(body).encode() if body is not None else None, params=params
    )
    if status == 404:
        raise NotFoundError("The custom resource or API is no longer available")
    if status == 403:
        raise PermissionDeniedError("Cluster credentials do not allow this custom-resource action")
    if status == 410:
        raise ConflictError("The resource list snapshot expired; refresh the list")
    if status == 409:
        raise ConflictError("Resource or definition changed, or the resource already exists; refresh and review again")
    if status in {400, 422}:
        fields = []
        try:
            for cause in json.loads(data).get("details", {}).get("causes", [])[:50]:
                field = cause.get("field", "")
                if isinstance(field, str) and re.fullmatch(r"[a-zA-Z0-9_.\[\]-]{1,256}", field):
                    fields.append(field)
        except (ValueError, TypeError, AttributeError):
            pass
        raise ValidationFailedError(
            "Kubernetes rejected the resource. Check the schema and reported field paths; response values withheld.",
            details={"fields": fields},
        )
    if status >= 400:
        raise kc.KubernetesAPIError("The custom-resource API request failed; raw response withheld", http_status=status)
    if not data:
        return {}
    try:
        return json.loads(data)
    except ValueError:
        raise InvalidStateError("Kubernetes returned an unsupported custom-resource response") from None


def conditions(obj):
    result = []
    status = obj.get("status") if isinstance(obj.get("status"), dict) else {}
    items = status.get("conditions")
    if not isinstance(items, list):
        return []
    for item in items[:50]:
        if not isinstance(item, dict):
            continue

        def identifier(value):
            return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}", value) else None

        state = item.get("status")
        result.append(
            {
                "type": identifier(item.get("type")) or "Unknown",
                "status": state if isinstance(state, str) and state in {"True", "False", "Unknown"} else "Unknown",
                "last_transition_time": item.get("lastTransitionTime")
                if isinstance(item.get("lastTransitionTime"), str)
                and re.fullmatch(r"[0-9TZ:.+-]{10,64}", item["lastTransitionTime"])
                else None,
            }
        )
    return result


def summary(obj):
    meta = obj.get("metadata") or {}
    status = obj.get("status") if isinstance(obj.get("status"), dict) else {}
    observed = status.get("observedGeneration")
    return {
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "uid": meta.get("uid"),
        "resource_version": meta.get("resourceVersion"),
        "generation": meta.get("generation"),
        "created_at": meta.get("creationTimestamp"),
        "deletion_timestamp": meta.get("deletionTimestamp"),
        "observed_generation": observed if isinstance(observed, int) else None,
        "conditions": conditions(obj),
        "finalizer_count": len(meta.get("finalizers") or []),
        "owner_count": len(meta.get("ownerReferences") or []),
    }


def definition_summary(obj):
    spec = obj.get("spec") or {}
    names = spec.get("names") or {}
    return {
        "name": obj["metadata"]["name"],
        "uid": obj["metadata"]["uid"],
        "group": spec.get("group"),
        "kind": names.get("kind"),
        "plural": names.get("plural"),
        "scope": spec.get("scope"),
        "versions": [
            {"name": v["name"], "served": bool(v.get("served")), "storage": bool(v.get("storage"))}
            for v in spec.get("versions", [])
        ],
        "conditions": conditions(obj),
        "deletion_timestamp": obj["metadata"].get("deletionTimestamp"),
    }


def stamp(definition):
    return {
        "uid": definition["metadata"]["uid"],
        "spec_hash": hashlib.sha256(json.dumps(definition["spec"], sort_keys=True).encode()).hexdigest(),
    }


def resolve(api, name, version, namespace=None, *, served=True):
    definition = request(api, "GET", BASE + "/" + name)
    spec = definition.get("spec") or {}
    names = spec.get("names") or {}
    group = spec.get("group", "")
    plural = names.get("plural", "")
    kind = names.get("kind", "")
    if (
        not isinstance(group, str)
        or not group
        or len(group) > 253
        or not all(re.fullmatch(DNS, label) for label in group.split("."))
        or not isinstance(plural, str)
        or not re.fullmatch(DNS, plural)
        or len(plural) > 63
        or not isinstance(kind, str)
        or not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,62}", kind)
        or name != plural + "." + group
        or definition["metadata"]["name"] != name
        or spec.get("scope") not in {"Namespaced", "Cluster"}
    ):
        raise InvalidStateError("The definition advertises an unsupported API identity")
    selected = next((item for item in spec.get("versions", []) if item.get("name") == version), None)
    if selected is None:
        raise NotFoundError("The selected CRD version does not exist")
    namespaced = spec["scope"] == "Namespaced"
    if not namespaced and namespace is not None:
        raise ValidationFailedError("Cluster-scoped custom resources cannot use a namespace")
    if served:
        cond = {c.get("type"): c.get("status") for c in (definition.get("status") or {}).get("conditions", [])}
        if (
            not selected.get("served")
            or cond.get("Established") != "True"
            or cond.get("NamesAccepted") == "False"
            or definition["metadata"].get("deletionTimestamp")
        ):
            raise InvalidStateError("This definition/version is not established and served")
        listing = request(api, "GET", f"/apis/{group}/{version}")
        resource = next((item for item in listing.get("resources", []) if item.get("name") == plural), None)
        if resource is None or resource.get("kind") != kind or bool(resource.get("namespaced")) != namespaced:
            raise InvalidStateError("Live API discovery does not match the selected CRD")
    else:
        resource = None
    base = f"/apis/{group}/{version}"
    collection = base + (f"/namespaces/{namespace}" if namespaced and namespace else "") + "/" + plural
    return definition, selected, collection, resource


def schema_fields(schema):
    rows: list[dict[str, Any]] = []
    truncated = False

    def visit(node, path, required, depth):
        nonlocal truncated
        if len(rows) >= 500 or depth > 12:
            truncated = True
            return
        if not isinstance(node, dict):
            return
        row = {
            "path": path,
            "type": node.get("type", "unspecified"),
            "required": required,
            "nullable": bool(node.get("nullable")),
            "preserve_unknown": bool(node.get("x-kubernetes-preserve-unknown-fields")),
            "enum_count": len(node["enum"]) if isinstance(node.get("enum"), list) else 0,
            "validation_rule_count": len(node.get("x-kubernetes-validations") or []),
        }
        for key in ("minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems"):
            if isinstance(node.get(key), (int, float)):
                row[key] = node[key]
        rows.append(row)
        for key, child in (node.get("properties") or {}).items():
            visit(child, path + "." + key, key in node.get("required", []), depth + 1)
        if isinstance(node.get("items"), dict):
            visit(node["items"], path + "[]", False, depth + 1)
        if isinstance(node.get("additionalProperties"), dict):
            visit(node["additionalProperties"], path + "{}", False, depth + 1)

    visit(schema, "$", True, 0)
    return {"fields": rows, "truncated": truncated, "has_schema": bool(schema)}


def template(definition, selected, namespace):
    schema = (selected.get("schema") or {}).get("openAPIV3Schema") or {}
    budget = [100]

    def seed(node, depth=0):
        budget[0] -= 1
        if depth > 8 or budget[0] <= 0:
            return None
        kind = node.get("type")
        if kind == "object":
            return {
                key: seed(child, depth + 1)
                for key, child in (node.get("properties") or {}).items()
                if key in node.get("required", []) and key not in {"status", "metadata", "apiVersion", "kind"}
            }
        return {"string": "", "integer": 0, "number": 0, "boolean": False, "array": []}.get(kind, {})

    obj = seed(schema) or {}
    obj.update(
        apiVersion=definition["spec"]["group"] + "/" + selected["name"],
        kind=definition["spec"]["names"]["kind"],
        metadata={"name": "new-resource"},
    )
    if definition["spec"]["scope"] == "Namespaced":
        obj["metadata"]["namespace"] = namespace or "default"
    if "spec" in (schema.get("properties") or {}) and "spec" not in obj:
        obj["spec"] = seed(schema["properties"]["spec"])
    return yaml.safe_dump(obj, sort_keys=False)


def editable(obj):
    result = copy.deepcopy(obj)
    result.pop("status", None)
    result["metadata"] = {
        k: v for k, v in result["metadata"].items() if k in {"name", "namespace", "labels", "annotations"}
    }
    ann = result["metadata"].get("annotations") or {}
    for key in (MARKER, "kubectl.kubernetes.io/last-applied-configuration"):
        ann.pop(key, None)
    if ann:
        result["metadata"]["annotations"] = ann
    else:
        result["metadata"].pop("annotations", None)
    return result


def fingerprint(obj):
    return hashlib.sha256(comparable(obj).encode()).hexdigest()


def optional(api, path):
    try:
        return request(api, "GET", path)
    except NotFoundError:
        return None


def parse_manifest(text, target, definition):
    validate_mapping(text)  # Bounded YAML mapping, safe parser messages and no cycles.
    try:
        obj = yaml.safe_load(text)
    except yaml.YAMLError:
        raise ValidationFailedError("Invalid custom-resource YAML; parser excerpts withheld") from None
    if not isinstance(obj, dict):
        raise ValidationFailedError("Provide one Kubernetes object")
    if (
        obj.get("apiVersion") != definition["spec"]["group"] + "/" + target.version
        or obj.get("kind") != definition["spec"]["names"]["kind"]
    ):
        raise ValidationFailedError("YAML API version/kind differs from the selected CRD")
    meta = obj.get("metadata")
    if not isinstance(meta, dict) or meta.get("name") != target.name or meta.get("namespace") != target.namespace:
        raise ValidationFailedError("YAML name/namespace differs from the reviewed target")
    if "status" in obj or any(k not in {"name", "namespace", "labels", "annotations"} for k in meta):
        raise ValidationFailedError("Status and server/ownership metadata are read-only; use the edit-ready YAML")
    for key in ("labels", "annotations"):
        if key in meta and (not isinstance(meta[key], dict) or any(not isinstance(v, str) for v in meta[key].values())):
            raise ValidationFailedError("Labels and annotations must contain string values")
    if MARKER in (meta.get("annotations") or {}):
        raise ValidationFailedError("The custom-resource review marker is reserved")
    return obj


def candidate(live, desired, marker):
    result = copy.deepcopy(desired)
    meta = result["metadata"]
    if live:
        original = live["metadata"]
        for key in ("labels", "annotations"):
            values = meta.setdefault(key, {})
            for name, value in (original.get(key) or {}).items():
                if name.startswith(("kplatform.io/", "meta.helm.sh/")) or name == "app.kubernetes.io/managed-by":
                    if name != MARKER and name in values and values[name] != value:
                        raise InvalidStateError("Existing controller ownership markers cannot be changed here")
                    values[name] = value
        updated = copy.deepcopy(original)
        updated.pop("managedFields", None)
        updated.update(meta)
        meta = updated
        result["metadata"] = meta
        if "status" in live:
            result["status"] = copy.deepcopy(live["status"])
    meta.setdefault("annotations", {})[MARKER] = str(marker)
    return result


def changed_fields(before, after):
    rows: list[str] = []

    def visit(a, b, path, depth=0):
        if len(rows) >= 100 or depth > 16:
            return
        if a == b:
            return
        if isinstance(a, dict) and isinstance(b, dict):
            for key in sorted(a.keys() | b.keys()):
                visit(a.get(key), b.get(key), path + "/" + key, depth + 1)
        else:
            rows.append(path or "/")

    visit(before or {}, after or {}, "")
    return rows


class CustomResources:
    def __init__(self, kubernetes):
        self.kubernetes = kubernetes
        self.session = kubernetes.session
        self.store = kubernetes.secret_store

    def definitions(self, principal, cluster_id):
        from app.modules.kubernetes.definition_lifecycle import managed

        _, api = self.kubernetes.api(principal, cluster_id)
        items = []
        truncated = False
        token = None
        seen = set()
        try:
            for _ in range(10):
                page = request(api, "GET", BASE, params={"limit": 100, **({"continue": token} if token else {})})
                values = page.get("items", [])
                if not isinstance(values, list):
                    raise InvalidStateError("Unsupported CRD inventory response")
                items.extend(values)
                token = (page.get("metadata") or {}).get("continue")
                if len(items) > 1000:
                    items = items[:1000]
                    truncated = True
                if not token:
                    break
                if not isinstance(token, str) or len(token) > 8192 or token in seen or len(items) >= 1000:
                    truncated = True
                    break
                seen.add(token)
            else:
                truncated = True
        except PermissionDeniedError:
            return {"available": False, "reason": "forbidden", "items": [], "truncated": False}
        except NotFoundError:
            return {"available": False, "reason": "unavailable", "items": [], "truncated": False}
        return {
            "available": True,
            "items": sorted(
                [{**definition_summary(obj), "managed": managed(obj, cluster_id)} for obj in items],
                key=lambda obj: obj["name"],
            ),
            "truncated": truncated,
        }

    def definition(self, principal, cluster_id, name, version, namespace=None):
        _, api = self.kubernetes.api(principal, cluster_id)
        definition, selected, _path, _resource = resolve(api, name, version, namespace, served=False)
        schema = (selected.get("schema") or {}).get("openAPIV3Schema") or {}
        return {
            **definition_summary(definition),
            "selected_version": version,
            "schema": schema_fields(schema),
            "subresources": sorted((selected.get("subresources") or {}).keys()),
            "template": template(definition, selected, namespace),
        }

    def reveal_schema(self, principal, cluster_id, name, version, expected_uid=None):
        require(principal, "secrets.reveal")
        _, api = self.kubernetes.api(principal, cluster_id)
        definition, selected, _path, _resource = resolve(api, name, version, served=False)
        if expected_uid and definition["metadata"]["uid"] != expected_uid:
            raise ConflictError("The CRD definition was replaced; refresh before revealing its schema")
        audit.record(
            self.session,
            principal=principal,
            action="custom_resource.schema.reveal",
            resource_type="kubernetes.custom_resource_definition",
            resource_id=f"{cluster_id}/{name}/{version}",
            details={"uid": definition["metadata"]["uid"]},
        )
        self.session.commit()
        return {"yaml": yaml.safe_dump((selected.get("schema") or {}).get("openAPIV3Schema") or {}, sort_keys=False)}

    def listing(self, principal, cluster_id, name, version, namespace=None, token=None):
        _, api = self.kubernetes.api(principal, cluster_id)
        definition, _selected, path, resource = resolve(api, name, version, namespace)
        params = {"limit": 100}
        if token:
            params["continue"] = token
        result = request(api, "GET", path, params=params)
        items = result.get("items", [])
        if not isinstance(items, list):
            raise InvalidStateError("Unsupported custom-resource list response")
        return {
            "items": [summary(obj) for obj in items[:100]],
            "continue_token": (result.get("metadata") or {}).get("continue") or None,
            "truncated": len(items) > 100,
            "verbs": resource.get("verbs", []),
            "scope": definition["spec"]["scope"],
        }

    def read(self, principal, cluster_id, target):
        _, api = self.kubernetes.api(principal, cluster_id)
        definition, selected, collection, _resource = resolve(api, target.definition, target.version, target.namespace)
        if target.definition_uid and target.definition_uid != definition["metadata"]["uid"]:
            raise ConflictError("The CRD definition was replaced; refresh the selection")
        if definition["spec"]["scope"] == "Namespaced" and not target.namespace:
            raise ValidationFailedError("Select a namespace for this resource")
        obj = request(api, "GET", collection + "/" + target.name)
        if target.expected_uid and target.expected_uid != obj["metadata"]["uid"]:
            raise ConflictError("The resource was replaced; refresh the selection")
        return obj, definition, selected

    def detail(self, principal, cluster_id, target):
        obj, _definition, _selected = self.read(principal, cluster_id, target)
        return {
            "summary": summary(obj),
            "notice": "Custom-resource content is withheld until an authorized, audited reveal.",
        }

    def reveal(self, principal, cluster_id, target):
        require(principal, "secrets.reveal")
        obj, _definition, _selected = self.read(principal, cluster_id, target)
        audit.record(
            self.session,
            principal=principal,
            action="custom_resource.reveal",
            resource_type="kubernetes.custom_resource",
            resource_id=f"{cluster_id}/{target.definition}/{target.namespace or '_cluster'}/{target.name}",
            details={"uid": obj["metadata"]["uid"], "version": target.version},
        )
        self.session.commit()
        return {
            "yaml": yaml.safe_dump(obj, sort_keys=False),
            "edit_yaml": yaml.safe_dump(editable(obj), sort_keys=False),
            "uid": obj["metadata"]["uid"],
            "content_hash": fingerprint(obj),
        }

    def review(self, principal, cluster_id, body: ReviewInput):
        cluster, api = self.kubernetes.api(principal, cluster_id)
        definition, _selected, collection, _resource = resolve(api, body.definition, body.version, body.namespace)
        if body.definition_uid != definition["metadata"]["uid"]:
            raise ConflictError("The selected CRD definition was replaced; refresh before reviewing")
        if definition["spec"]["scope"] == "Namespaced" and not body.namespace:
            raise ValidationFailedError("Choose an existing destination namespace")
        path = collection + "/" + body.name
        live = optional(api, path)
        if body.action == "create" and live:
            raise ConflictError("The custom resource already exists")
        if body.action != "create" and (
            not live or not body.expected_uid or live["metadata"]["uid"] != body.expected_uid
        ):
            raise ConflictError("The selected resource was replaced or is missing; refresh before reviewing")
        if live and live["metadata"].get("deletionTimestamp"):
            raise InvalidStateError("The resource is already being deleted")
        if body.action == "update" and body.expected_hash != fingerprint(live):
            raise ConflictError(
                "The resource changed since its YAML was revealed; reveal current YAML and review again"
            )
        identifier = uuid.uuid4()
        desired = None
        if body.action != "delete":
            desired = parse_manifest(body.manifest.get_secret_value(), body, definition)
        payload = (
            candidate(live, desired, identifier)
            if desired
            else {
                "apiVersion": "v1",
                "kind": "DeleteOptions",
                "dryRun": ["All"],
                "propagationPolicy": "Foreground",
                "preconditions": {
                    "uid": live["metadata"]["uid"],
                    "resourceVersion": live["metadata"]["resourceVersion"],
                },
            }
        )
        method = {"create": "POST", "update": "PUT", "delete": "DELETE"}[body.action]
        planned = request(
            api,
            method,
            collection if body.action == "create" else path,
            body=payload,
            params={"dryRun": "All", "fieldValidation": "Strict", "fieldManager": MANAGER},
        )
        sealed = self.store.seal(json.dumps(desired).encode())
        specification = {
            **body.model_dump(exclude={"manifest"}),
            "definition_stamp": stamp(definition),
            "live_uid": live["metadata"]["uid"] if live else None,
            "live_hash": fingerprint(live),
            "planned_hash": fingerprint(planned) if body.action != "delete" else None,
        }
        row = CustomResourceReview(
            id=identifier,
            organization_id=cluster.organization_id,
            project_id=cluster.project_id,
            cluster_id=cluster.id,
            created_by=principal.subject,
            specification=specification,
            key_id=sealed.key_id,
            ciphertext=sealed.ciphertext,
            expires_at=datetime.now(UTC) + timedelta(minutes=15),
        )
        self.session.add(row)
        fields = changed_fields(
            editable(live) if live else None, editable(planned) if body.action != "delete" else None
        )
        audit.record(
            self.session,
            principal=principal,
            action="custom_resource.review",
            resource_type="kubernetes.custom_resource",
            resource_id=row.id,
            details={
                "action": body.action,
                "definition": body.definition,
                "name": body.name,
                "namespace": body.namespace,
                "version": body.version,
            },
        )
        self.session.commit()
        return {
            "id": row.id,
            "expires_at": row.expires_at,
            "action": body.action,
            "name": body.name,
            "namespace": body.namespace,
            "definition": body.definition,
            "version": body.version,
            "fields": fields,
            "notice": "Server dry run passed. Field paths only; values and raw admission responses are withheld.",
        }

    def execute(self, principal, cluster_id, body: ExecuteInput):
        _cluster, api = self.kubernetes.api(principal, cluster_id)
        row = self.session.scalar(
            select(CustomResourceReview)
            .where(CustomResourceReview.id == body.review_id, CustomResourceReview.cluster_id == cluster_id)
            .with_for_update()
        )
        if row is None or row.created_by != principal.subject or row.specification.get("category") is not None:
            raise NotFoundError("Custom-resource review not found")
        spec = row.specification
        if body.confirm != spec["name"]:
            raise ConfirmationRequiredError("Repeat the resource name to confirm this action")
        if row.result:
            return row.result
        if row.expires_at < datetime.now(UTC):
            raise InvalidStateError("Custom-resource review expired; review again")
        target = Target.model_validate(spec)
        definition, _selected, collection, _resource = resolve(api, target.definition, target.version, target.namespace)
        if stamp(definition) != spec["definition_stamp"]:
            raise ConflictError("The CRD definition changed after review; review again")
        path = collection + "/" + target.name
        live = optional(api, path)
        action = spec["action"]
        already = live is not None and (live["metadata"].get("annotations") or {}).get(MARKER) == str(row.id)
        if already and action != "delete":
            if fingerprint(live) != spec["planned_hash"]:
                raise ConflictError("The resource changed after this action applied; refresh and review again")
            result = {"action": action, "status": "already_applied", "uid": live["metadata"]["uid"]}
        elif action == "delete" and live is None:
            result = {"action": action, "status": "absent", "uid": spec["live_uid"]}
        else:
            if (live["metadata"]["uid"] if live else None) != spec["live_uid"]:
                raise ConflictError("The resource identity changed after review")
            deleting = bool(live and live["metadata"].get("deletionTimestamp"))
            if not (action == "delete" and deleting) and fingerprint(live) != spec["live_hash"]:
                raise ConflictError("The resource changed after review; review again")
            desired = json.loads(self.store.unseal(SealedSecret(row.key_id, row.ciphertext)))
            if action == "delete":
                if not deleting:
                    request(
                        api,
                        "DELETE",
                        path,
                        body={
                            "apiVersion": "v1",
                            "kind": "DeleteOptions",
                            "propagationPolicy": "Foreground",
                            "preconditions": {
                                "uid": spec["live_uid"],
                                "resourceVersion": live["metadata"]["resourceVersion"],
                            },
                        },
                    )
                result = {"action": action, "status": "deletion_requested", "uid": spec["live_uid"]}
            else:
                payload = candidate(live, desired, row.id)
                actual = request(
                    api,
                    "POST" if action == "create" else "PUT",
                    collection if action == "create" else path,
                    body=payload,
                    params={"fieldValidation": "Strict", "fieldManager": MANAGER},
                )
                result = {
                    "action": action,
                    "status": "created" if action == "create" else "updated",
                    "uid": actual["metadata"]["uid"],
                }
        row.result = {
            **result,
            "name": spec["name"],
            "namespace": spec["namespace"],
            "definition": spec["definition"],
            "version": spec["version"],
        }
        audit.record(
            self.session,
            principal=principal,
            action="custom_resource." + action,
            resource_type="kubernetes.custom_resource",
            resource_id=row.id,
            details=row.result,
        )
        self.session.commit()
        return row.result
