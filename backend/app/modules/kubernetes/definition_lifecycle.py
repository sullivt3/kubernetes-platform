"""Reviewed lifecycle of platform-installed CustomResourceDefinitions."""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

import yaml
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import select

from app.core.errors import (
    ConfirmationRequiredError,
    ConflictError,
    InvalidStateError,
    NotFoundError,
    ValidationFailedError,
)
from app.core.secrets import SealedSecret
from app.modules.auditing import service as audit
from app.modules.helm.catalog import values as validate_mapping
from app.modules.kubernetes.custom_resource_models import CustomResourceReview
from app.modules.kubernetes.custom_resources import (
    BASE,
    DNS,
    MARKER,
    NAME,
    VERSION,
    CustomResources,
    candidate,
    changed_fields,
    editable,
    fingerprint,
    optional,
    request,
    resolve,
)
from app.modules.organizations.policy import require

OWNER = "kplatform.io/crd-cluster"
MANAGER = "kplatform.io/crd-manager"
MANAGER_VALUE = "kubernetes-platform"
CATEGORY = "definition"
API_VERSION = "apiextensions.k8s.io/v1"


class DefinitionTarget(BaseModel):
    name: str = Field(pattern=NAME, max_length=253)
    expected_uid: str | None = Field(default=None, min_length=1, max_length=128)


class DefinitionReviewInput(DefinitionTarget):
    action: Literal["install", "update", "remove"]
    manifest: SecretStr = SecretStr("")
    expected_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


def managed(definition, cluster_id):
    meta = definition.get("metadata") or {}
    labels = meta.get("labels") or {}
    annotations = meta.get("annotations") or {}
    return (
        labels.get(OWNER) == str(cluster_id)
        and labels.get(MANAGER) == MANAGER_VALUE
        and labels.get("app.kubernetes.io/managed-by") in {None, MANAGER_VALUE}
        and not any(k.startswith("meta.helm.sh/") or "gitops" in k for k in annotations.keys() | labels.keys())
        and not meta.get("ownerReferences")
    )


def check_ownership(definition, cluster_id):
    if not managed(definition, cluster_id):
        raise InvalidStateError(
            "Only definitions installed by this platform for this cluster can be updated or removed here"
        )
    if definition["metadata"].get("deletionTimestamp"):
        raise InvalidStateError("The definition is already being removed")


def parse_definition(text, name):
    validate_mapping(text)  # Bounded single YAML mapping; parser errors never expose submitted values.
    obj = yaml.safe_load(text)
    if (
        not isinstance(obj, dict)
        or obj.get("apiVersion") != API_VERSION
        or obj.get("kind") != "CustomResourceDefinition"
    ):
        raise ValidationFailedError("Provide one apiextensions.k8s.io/v1 CustomResourceDefinition")
    meta = obj.get("metadata")
    if (
        not isinstance(meta, dict)
        or meta.get("name") != name
        or any(k not in {"name", "labels", "annotations"} for k in meta)
    ):
        raise ValidationFailedError(
            "Definition name must match; namespace, status and server/ownership metadata are read-only"
        )
    if set(obj) - {"apiVersion", "kind", "metadata", "spec"}:
        raise ValidationFailedError("Only apiVersion, kind, metadata and spec are editable")
    for key in ("labels", "annotations"):
        values = meta.get(key, {})
        if not isinstance(values, dict) or any(
            not isinstance(k, str) or not isinstance(v, str) for k, v in values.items()
        ):
            raise ValidationFailedError("Definition labels and annotations must contain string values")
        if any(k.startswith(("kplatform.io/", "meta.helm.sh/")) or k == "app.kubernetes.io/managed-by" for k in values):
            raise ValidationFailedError("Controller ownership markers are reserved; use edit-ready definition YAML")
    spec = obj.get("spec")
    if not isinstance(spec, dict):
        raise ValidationFailedError("A definition specification is required")
    group = spec.get("group")
    names = spec.get("names")
    if (
        not isinstance(group, str)
        or len(group) > 253
        or not all(re.fullmatch(DNS, label) for label in group.split("."))
    ):
        raise ValidationFailedError("Provide a valid definition API group")
    if (
        not isinstance(names, dict)
        or not isinstance(names.get("plural"), str)
        or not re.fullmatch(DNS, names["plural"])
        or len(names["plural"]) > 63
        or name != names["plural"] + "." + group
    ):
        raise ValidationFailedError("Definition name must equal plural.group")
    kind = names.get("kind")
    if not isinstance(kind, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,62}", kind):
        raise ValidationFailedError("Provide a kind supported by the custom-resource explorer")
    if spec.get("scope") not in {"Namespaced", "Cluster"}:
        raise ValidationFailedError("Definition scope must be Namespaced or Cluster")
    versions = spec.get("versions")
    if not isinstance(versions, list) or not versions or len(versions) > 20:
        raise ValidationFailedError("Provide between one and twenty definition versions")
    names_seen = set()
    for version in versions:
        if (
            not isinstance(version, dict)
            or not isinstance(version.get("name"), str)
            or not re.fullmatch(VERSION, version["name"])
            or version["name"] in names_seen
        ):
            raise ValidationFailedError("Definition version names must be valid and unique")
        names_seen.add(version["name"])
        if not isinstance(version.get("served"), bool) or not isinstance(version.get("storage"), bool):
            raise ValidationFailedError("Each version must declare served and storage booleans")
    storage = [v for v in versions if v["storage"]]
    if len(storage) != 1 or not storage[0]["served"]:
        raise ValidationFailedError("Exactly one served version must be the storage version")
    conversion = spec.get("conversion") or {"strategy": "None"}
    if not isinstance(conversion, dict) or conversion != {"strategy": "None"}:
        raise InvalidStateError(
            "Conversion webhooks require a separate operator/migration workflow; only None conversion is supported here"
        )
    return obj


def editor_definition(obj):
    result = editable(obj)
    for key in ("labels", "annotations"):
        values = result["metadata"].get(key) or {}
        result["metadata"][key] = {
            k: v
            for k, v in values.items()
            if not k.startswith(("kplatform.io/", "meta.helm.sh/")) and k != "app.kubernetes.io/managed-by"
        }
        if not result["metadata"][key]:
            result["metadata"].pop(key)
    return result


def preserve_versions(live, desired):
    before, after = live["spec"], desired["spec"]
    if any(before.get(key) != after.get(key) for key in ("group", "names", "scope")):
        raise InvalidStateError("API group, names and scope cannot be changed here")
    old_versions = {v["name"]: v for v in before["versions"]}
    new_versions = {v["name"]: v for v in after["versions"]}
    old_storage = next((v["name"] for v in before["versions"] if v.get("storage")), None)
    new_storage = next(v["name"] for v in after["versions"] if v["storage"])
    stored = set((live.get("status") or {}).get("storedVersions") or [])
    if old_storage != new_storage or not (old_versions.keys() | stored) <= new_versions.keys():
        raise InvalidStateError(
            "Storage-version changes and version removal require a separate verified storage migration"
        )
    if any(v.get("served") and not new_versions[name]["served"] for name, v in old_versions.items()):
        raise InvalidStateError("Previously served versions must remain served to preserve API access")
    if (before.get("conversion") or {"strategy": "None"}) != (after.get("conversion") or {"strategy": "None"}):
        raise InvalidStateError("Conversion configuration cannot be changed here")


def ensure_empty(api, live):
    versions = live["spec"]["versions"]
    version = next((v["name"] for v in versions if v.get("storage") and v.get("served")), None)
    if version is None:
        raise InvalidStateError("An established served storage version is required to verify instance inventory")
    _, _, collection, _ = resolve(api, live["metadata"]["name"], version)
    page = request(api, "GET", collection, params={"limit": 1})
    if not isinstance(page.get("items"), list):
        raise InvalidStateError("Instance inventory could not be verified")
    if (
        page["items"]
        or (page.get("metadata") or {}).get("continue")
        or (page.get("metadata") or {}).get("remainingItemCount")
    ):
        raise InvalidStateError(
            "Custom resource instances still exist; remove or migrate them before changing this definition"
        )


def delete_options(live, *, dry_run=False):
    result = {
        "apiVersion": "v1",
        "kind": "DeleteOptions",
        "propagationPolicy": "Foreground",
        "preconditions": {"uid": live["metadata"]["uid"], "resourceVersion": live["metadata"]["resourceVersion"]},
    }
    if dry_run:
        result["dryRun"] = ["All"]
    return result


class DefinitionLifecycle(CustomResources):
    def reveal_definition(self, principal, cluster_id, body: DefinitionTarget):
        require(principal, "secrets.reveal")
        _, api = self.kubernetes.api(principal, cluster_id)
        live = optional(api, BASE + "/" + body.name)
        if live is None or (body.expected_uid and body.expected_uid != live["metadata"]["uid"]):
            raise ConflictError("The definition was removed or replaced; refresh the selection")
        audit.record(
            self.session,
            principal=principal,
            action="custom_definition.reveal",
            resource_type="kubernetes.custom_resource_definition",
            resource_id=f"{cluster_id}/{body.name}",
            details={"uid": live["metadata"]["uid"]},
        )
        self.session.commit()
        return {
            "yaml": yaml.safe_dump(live, sort_keys=False),
            "edit_yaml": yaml.safe_dump(editor_definition(live), sort_keys=False),
            "uid": live["metadata"]["uid"],
            "content_hash": fingerprint(live),
        }

    def review_definition(self, principal, cluster_id, body: DefinitionReviewInput):
        require(principal, "organization.manage")
        require(principal, "clusters.write")
        cluster, api = self.kubernetes.api(principal, cluster_id)
        path = BASE + "/" + body.name
        live = optional(api, path)
        if body.action == "install":
            if live:
                raise ConflictError("The definition already exists; this action never adopts existing definitions")
        else:
            if live is None or not body.expected_uid or body.expected_uid != live["metadata"]["uid"]:
                raise ConflictError("The selected definition was replaced or is missing; refresh before reviewing")
            check_ownership(live, cluster_id)
        if body.action == "update" and body.expected_hash != fingerprint(live):
            raise ConflictError("Definition changed since reveal; reveal current YAML and review again")
        identifier = uuid.uuid4()
        desired = parse_definition(body.manifest.get_secret_value(), body.name) if body.action != "remove" else None
        if desired:
            desired["metadata"].setdefault("labels", {}).update({OWNER: str(cluster_id), MANAGER: MANAGER_VALUE})
        if body.action == "update":
            preserve_versions(live, desired)
        needs_empty = body.action == "remove" or (
            body.action == "update" and desired is not None and live["spec"] != desired["spec"]
        )
        if needs_empty:
            ensure_empty(api, live)
        payload = candidate(live, desired, identifier) if desired else delete_options(live, dry_run=True)
        method = {"install": "POST", "update": "PUT", "remove": "DELETE"}[body.action]
        planned = request(
            api,
            method,
            BASE if body.action == "install" else path,
            body=payload,
            params={"dryRun": "All", "fieldValidation": "Strict"},
        )
        sealed = self.store.seal(json.dumps(desired).encode())
        spec = {
            "category": CATEGORY,
            "action": body.action,
            "definition": body.name,
            "live_uid": live["metadata"]["uid"] if live else None,
            "live_hash": fingerprint(live),
            "planned_hash": fingerprint(planned) if desired else None,
            "needs_empty": needs_empty,
            "stored_versions": (live.get("status") or {}).get("storedVersions", []) if live else [],
        }
        row = CustomResourceReview(
            id=identifier,
            cluster_id=cluster.id,
            organization_id=cluster.organization_id,
            project_id=cluster.project_id,
            created_by=principal.subject,
            specification=spec,
            key_id=sealed.key_id,
            ciphertext=sealed.ciphertext,
            expires_at=datetime.now(UTC) + timedelta(minutes=15),
        )
        self.session.add(row)
        self.record(principal, cluster_id, row, "review")
        self.session.commit()
        return {
            "id": row.id,
            "name": body.name,
            "action": body.action,
            "expires_at": row.expires_at,
            "fields": changed_fields(
                editor_definition(live) if live else None, editor_definition(planned) if desired else None
            ),
            "requires_empty": needs_empty,
            "notice": "Server dry run passed. Values withheld. Definition acceptance does not establish API readiness.",
        }

    def execute_definition(self, principal, cluster_id, body):
        require(principal, "organization.manage")
        require(principal, "clusters.write")
        _, api = self.kubernetes.api(principal, cluster_id)
        row = self.session.scalar(
            select(CustomResourceReview)
            .where(CustomResourceReview.id == body.review_id, CustomResourceReview.cluster_id == cluster_id)
            .with_for_update()
        )
        if row is None or row.created_by != principal.subject or row.specification.get("category") != CATEGORY:
            raise NotFoundError("Definition review not found")
        spec = row.specification
        if body.confirm != spec["definition"]:
            raise ConfirmationRequiredError("Repeat the full definition name to confirm")
        if row.result:
            return row.result
        if row.expires_at < datetime.now(UTC):
            raise InvalidStateError("Definition review expired; review again")
        path = BASE + "/" + spec["definition"]
        live = optional(api, path)
        action = spec["action"]
        marker = ((live or {}).get("metadata", {}).get("annotations") or {}).get(MARKER)
        if action != "remove" and live and marker == str(row.id):
            check_ownership(live, cluster_id)
            if fingerprint(live) != spec["planned_hash"]:
                raise ConflictError("Definition changed after the reviewed action applied")
            result = {"status": "already_applied", "uid": live["metadata"]["uid"]}
        elif action == "remove" and live is None:
            result = {"status": "absent", "uid": spec["live_uid"]}
        else:
            if (live["metadata"]["uid"] if live else None) != spec["live_uid"]:
                raise ConflictError("Definition identity changed after review")
            if live:
                # Pending deletion is an accepted prior request; preserve finalizers and report it.
                if action == "remove" and live["metadata"].get("deletionTimestamp"):
                    if not managed(live, cluster_id):
                        raise ConflictError("Definition ownership changed")
                    result = {"status": "deletion_requested", "uid": spec["live_uid"]}
                    return self.finish(principal, cluster_id, row, result)
                check_ownership(live, cluster_id)
            if fingerprint(live) != spec["live_hash"]:
                raise ConflictError("Definition changed after review; review again")
            if live and ((live.get("status") or {}).get("storedVersions") or []) != spec["stored_versions"]:
                raise ConflictError("Stored versions changed after review; review again")
            desired = json.loads(self.store.unseal(SealedSecret(row.key_id, row.ciphertext)))
            if action == "update":
                preserve_versions(live, desired)
            if spec["needs_empty"]:
                ensure_empty(api, live)
            if action == "remove":
                request(api, "DELETE", path, body=delete_options(live))
                result = {"status": "deletion_requested", "uid": spec["live_uid"]}
            else:
                actual = request(
                    api,
                    "POST" if action == "install" else "PUT",
                    BASE if action == "install" else path,
                    body=candidate(live, desired, row.id),
                    params={"fieldValidation": "Strict"},
                )
                result = {"status": "installed" if action == "install" else "updated", "uid": actual["metadata"]["uid"]}
        return self.finish(principal, cluster_id, row, result)

    def record(self, principal, cluster_id, row, action):
        audit.record(
            self.session,
            principal=principal,
            action="custom_definition." + action,
            resource_type="kubernetes.custom_resource_definition",
            resource_id=f"{cluster_id}/{row.specification['definition']}",
            details={"review_id": str(row.id), "action": row.specification["action"]},
        )

    def finish(self, principal, cluster_id, row, result):
        row.result = {**result, "action": row.specification["action"], "name": row.specification["definition"]}
        self.record(principal, cluster_id, row, "execute")
        self.session.commit()
        return row.result
