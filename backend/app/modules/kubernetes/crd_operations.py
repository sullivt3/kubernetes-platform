"""Tracked CRD storage rewrites and service-backed conversion configuration."""

from __future__ import annotations

import base64
import copy
import json
import re
import time
from datetime import UTC, datetime
from typing import Any, Literal

from cryptography import x509
from pydantic import Field, SecretStr

from app.core.errors import ConflictError, InvalidStateError, PlatformError, ValidationFailedError
from app.modules.kubernetes import reviewed_operations as tasks
from app.modules.kubernetes.custom_resources import BASE, DNS, VERSION, fingerprint, request, resolve, stamp
from app.modules.kubernetes.definition_lifecycle import DefinitionTarget, check_ownership
from app.modules.organizations.policy import require
from app.orchestration.workflow import (
    OperationCancelledError,
    OperationTarget,
    StepDefinition,
    WorkflowDefinition,
    workflow_registry,
)

MIGRATION_MARKER = "kplatform.io/storage-migration"
CONVERSION_MARKER = "kplatform.io/conversion-review"
MAX_OBJECTS = 5000
MAX_PROBES = 200


class ReviewedDefinition(DefinitionTarget):
    expected_uid: str = Field(min_length=1, max_length=128)
    expected_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class MigrationInput(ReviewedDefinition):
    mode: Literal["certified", "rewrite_only"] = "certified"
    target_version: str = Field(pattern=VERSION)


class ConversionInput(ReviewedDefinition):
    action: Literal["configure", "probe"] = "configure"
    strategy: Literal["None", "Webhook"] = "Webhook"
    service_namespace: str = Field(default="default", pattern=DNS, max_length=63)
    service_name: str = Field(default="", pattern=r"^$|" + DNS, max_length=63)
    port: int = Field(default=443, ge=1, le=65535)
    path: str = Field(default="/convert", pattern=r"^/[A-Za-z0-9/_.-]{0,255}$")
    ca_pem: SecretStr = SecretStr("")


def baseline(service, principal, cluster_id, body):
    require(principal, "organization.manage")
    require(principal, "clusters.write")
    cluster, api = service.api(principal, cluster_id)
    live = request(api, "GET", BASE + "/" + body.name)
    check_ownership(live, cluster_id)
    if live["metadata"]["uid"] != body.expected_uid or fingerprint(live) != body.expected_hash:
        raise ConflictError("Definition changed since reveal; reload before reviewing")
    return cluster, api, live


def storage_version(live):
    return next(v["name"] for v in live["spec"]["versions"] if v.get("storage"))


def content(obj):
    result = copy.deepcopy(obj)
    result.pop("apiVersion", None)
    return fingerprint(result)


def pages(api, live, version, limit, guard=None):
    _, _, collection, _ = resolve(api, live["metadata"]["name"], version)
    token = None
    seen = set()
    count = 0
    page_count = 0
    while True:
        page_count += 1
        if page_count > 100:
            raise InvalidStateError("Instance pagination exceeded the bounded scan; no completion is certified")
        if guard:
            guard()
        page = request(api, "GET", collection, params={"limit": 100, **({"continue": token} if token else {})})
        if not isinstance(page.get("items"), list):
            raise InvalidStateError("Instance snapshot could not be verified")
        count += len(page["items"])
        if count > limit:
            raise InvalidStateError("Instance inventory exceeds this operation limit; no completion is certified")
        yield collection, page["items"]
        token = (page.get("metadata") or {}).get("continue")
        if not token:
            if (page.get("metadata") or {}).get("remainingItemCount"):
                raise InvalidStateError("Instance inventory is incomplete")
            break
        if not isinstance(token, str) or len(token) > 8192 or token in seen:
            raise InvalidStateError("Invalid instance pagination")
        seen.add(token)


def instance_path(collection, obj):
    meta = obj["metadata"]
    name = meta.get("name")
    namespace = meta.get("namespace")
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][-a-z0-9.]{0,252}", name):
        raise InvalidStateError("Unsupported instance identity")
    if namespace:
        if not isinstance(namespace, str) or not re.fullmatch(DNS, namespace):
            raise InvalidStateError("Unsupported instance namespace")
        base, plural = collection.rsplit("/", 1)
        return base + "/namespaces/" + namespace + "/" + plural + "/" + name
    return collection + "/" + name


def unchanged_dry_run(api, path, obj):
    desired = copy.deepcopy(obj)
    desired["metadata"].pop("managedFields", None)
    response = request(api, "PUT", path, body=desired, params={"dryRun": "All", "fieldValidation": "Strict"})
    if response.get("metadata", {}).get("uid") != obj["metadata"]["uid"] or content(response) != content(obj):
        raise InvalidStateError(
            "Rewrite admission/conversion changes object content; migration requires a compatible conversion and schema"
        )
    return desired


NATIVE = "/apis/storagemigration.k8s.io/v1/storageversionmigrations"


def manual_topology(api):
    pods = request(
        api,
        "GET",
        "/api/v1/namespaces/kube-system/pods",
        params={"labelSelector": "component=kube-apiserver", "limit": 100},
    )
    nodes = request(api, "GET", "/api/v1/nodes", params={"limit": 100})
    if (pods.get("metadata") or {}).get("continue") or (nodes.get("metadata") or {}).get("continue"):
        raise InvalidStateError("Control-plane topology inventory is incomplete")
    control = [
        n
        for n in nodes.get("items", [])
        if any(
            k in n.get("metadata", {}).get("labels", {})
            for k in ["node-role.kubernetes.io/control-plane", "node-role.kubernetes.io/master"]
        )
    ]
    items = pods.get("items", [])
    if (
        len(items) != 1
        or len(control) != 1
        or not items[0].get("metadata", {}).get("annotations", {}).get("kubernetes.io/config.mirror")
        or items[0].get("spec", {}).get("nodeName") != control[0]["metadata"]["name"]
    ):
        raise InvalidStateError(
            "Manual migration needs one observable mirrored API server/control-plane node. "
            "Enable native migration for HA or hidden control planes"
        )
    return {"pod_uid": items[0]["metadata"]["uid"], "node_uid": control[0]["metadata"]["uid"]}


def migration_backend(api, definition):
    status, data = api.request("GET", "/apis/storagemigration.k8s.io/v1")
    if status == 200:
        listing = json.loads(data)
        if any(r.get("name") == "storageversionmigrations" for r in listing.get("resources", [])):
            probe: dict[str, Any] = {
                "apiVersion": "storagemigration.k8s.io/v1",
                "kind": "StorageVersionMigration",
                "metadata": {"generateName": "kplatform-review-"},
                "spec": {
                    "resource": {
                        "group": definition["spec"]["group"],
                        "resource": definition["spec"]["names"]["plural"],
                    }
                },
            }
            request(api, "POST", NATIVE, body=probe, params={"dryRun": "All", "fieldValidation": "Strict"})
            active = request(api, "GET", NATIVE, params={"limit": 1000})
            if (active.get("metadata") or {}).get("continue"):
                raise InvalidStateError("Native migration inventory is incomplete")
            for item in active.get("items", []):
                if item.get("spec", {}).get("resource") == probe["spec"]["resource"] and not any(
                    c.get("type") in {"Succeeded", "Failed"} and c.get("status") == "True"
                    for c in ((item.get("status") or {}).get("conditions") or [])
                ):
                    raise ConflictError("A native migration for this resource is still active")
            return "native", None
    elif status not in {403, 404}:
        raise InvalidStateError("Storage migration API discovery failed")
    raise InvalidStateError(
        "Native migration API/controller is required for storage certification. "
        "Explicit rewrite-only mode retains old stored-version metadata."
    )


def native_rewrite(ctx, payload):
    import time

    api, _, spec = tasks.access(ctx, "storage_migration")
    name = "kplatform-" + ctx.params.review_id.hex
    path = NATIVE + "/" + name
    desired = {
        "apiVersion": "storagemigration.k8s.io/v1",
        "kind": "StorageVersionMigration",
        "metadata": {"name": name, "labels": {"kplatform.io/migration-review": str(ctx.params.review_id)}},
        "spec": {
            "resource": {
                "group": payload["original"]["spec"]["group"],
                "resource": payload["original"]["spec"]["names"]["plural"],
            }
        },
    }
    status, _ = api.request("GET", path)
    if status == 404:
        migration_backend(api, payload["original"])
    live = request(api, "POST", NATIVE, body=desired) if status == 404 else request(api, "GET", path)
    started = spec.get("progress", {}).get("native_started_at") or datetime.now(UTC).isoformat()
    expected_uid = spec.get("progress", {}).get("native_uid") or live["metadata"]["uid"]
    tasks.progress(ctx, native_uid=expected_uid, native_started_at=started, native_name=name)
    try:
        while True:
            migration_live(ctx, payload)
            if (
                live["metadata"]["uid"] != expected_uid
                or live["spec"] != desired["spec"]
                or live["metadata"].get("labels", {}).get("kplatform.io/migration-review") != str(ctx.params.review_id)
            ):
                raise ConflictError("Native migration resource was replaced or changed; no adoption is allowed")
            conditions = (live.get("status") or {}).get("conditions") or []
            if any(c.get("type") == "Failed" and c.get("status") == "True" for c in conditions):
                raise InvalidStateError("Native storage migration failed; raw controller output withheld")
            if any(c.get("type") == "Succeeded" and c.get("status") == "True" for c in conditions):
                tasks.progress(ctx, native_succeeded=True, scan_complete=True)
                return {"backend": "native", "migration_uid": expected_uid}
            if (datetime.now(UTC) - datetime.fromisoformat(started)).total_seconds() > 600:
                raise InvalidStateError("Native migration timed out; its resource is retained for diagnosis")
            time.sleep(1)
            tasks.access(ctx, "storage_migration")
            live = request(api, "GET", path)
    except OperationCancelledError:
        current = request(api, "GET", path)
        if (
            current["metadata"]["uid"] == expected_uid
            and current.get("spec") == desired["spec"]
            and current.get("metadata", {}).get("labels", {}).get("kplatform.io/migration-review")
            == str(ctx.params.review_id)
        ):
            request(
                api,
                "DELETE",
                path,
                body={
                    "apiVersion": "v1",
                    "kind": "DeleteOptions",
                    "preconditions": {"uid": expected_uid, "resourceVersion": current["metadata"]["resourceVersion"]},
                },
            )
            tasks.progress(ctx, native_cancel_requested=True, native_uid=None, native_started_at=None)
            ctx.info(
                "Requested removal of only the owned native migration; accepted controller writes cannot be undone"
            )
        raise


def review_migration(service, principal, cluster_id, body):
    cluster, api, live = baseline(service, principal, cluster_id, body)
    version = next((v for v in live["spec"]["versions"] if v["name"] == body.target_version and v.get("served")), None)
    if not version:
        raise InvalidStateError("Choose an existing served target version")
    conversion = live["spec"].get("conversion") or {"strategy": "None"}
    if conversion["strategy"] == "None":
        schema = version.get("schema")
        if any(v.get("schema") != schema for v in live["spec"]["versions"]):
            raise InvalidStateError(
                "None conversion needs identical version schemas; configure a webhook for different schemas"
            )
    else:
        check_service(api, conversion)
    if body.mode == "rewrite_only":
        if body.target_version != storage_version(live):
            raise InvalidStateError(
                "Rewrite-only mode must use the current storage version and never changes storage selection"
            )
        backend, topology = "manual-single-control-plane", manual_topology(api)
    else:
        backend, topology = migration_backend(api, live)
    count = 0
    for collection, objects in pages(api, live, body.target_version, MAX_OBJECTS):
        for obj in objects:
            unchanged_dry_run(api, instance_path(collection, obj), obj)
            count += 1
    payload = {
        "name": body.name,
        "uid": body.expected_uid,
        "before_hash": body.expected_hash,
        "original": live,
        "target": body.target_version,
        "backend": backend,
        "mode": body.mode,
        "topology": topology,
        "original_spec_hash": stamp(live)["spec_hash"],
    }
    return tasks.persist(
        service,
        principal,
        cluster,
        "storage_migration",
        body.name,
        payload,
        {
            "definition": body.name,
            "source_version": storage_version(live),
            "target_version": body.target_version,
            "reviewed_instances": count,
            "backend": backend,
            "mode": body.mode,
            "notice": "Switch storage, rewrite instances, then certify a complete guarded pass. "
            "Back up data and pause external definition writers.",
        },
    )


def migration_live(ctx, payload):
    api, _, spec = tasks.access(ctx, "storage_migration")
    live = request(api, "GET", BASE + "/" + payload["name"])
    check_ownership(live, ctx.params.cluster_id)
    if live["metadata"]["uid"] != payload["uid"]:
        raise ConflictError("Definition identity changed")
    desired = copy.deepcopy(payload["original"]["spec"])
    for version in desired["versions"]:
        version["storage"] = version["name"] == payload["target"]
    if live["spec"] != desired:
        raise ConflictError("Definition specification changed during migration")
    return api, live, spec


def prepare_migration(ctx):
    api, payload, _spec = tasks.access(ctx, "storage_migration")
    live = request(api, "GET", BASE + "/" + payload["name"])
    check_ownership(live, ctx.params.cluster_id)
    if live["metadata"]["uid"] != payload["uid"]:
        raise ConflictError("Definition was replaced")
    if payload["backend"] != "native" and manual_topology(api) != payload["topology"]:
        raise ConflictError("Control-plane topology changed since review")
    desired = copy.deepcopy(live)
    for version in desired["spec"]["versions"]:
        version["storage"] = version["name"] == payload["target"]
    marker = (live["metadata"].get("annotations") or {}).get(MIGRATION_MARKER)
    if marker != str(ctx.params.review_id):
        if fingerprint(live) != payload["before_hash"]:
            raise ConflictError("Definition changed after migration review")
        desired["metadata"].pop("managedFields", None)
        desired["metadata"].setdefault("annotations", {})[MIGRATION_MARKER] = str(ctx.params.review_id)
        request(
            api,
            "PUT",
            BASE + "/" + payload["name"],
            body=desired,
            params={"dryRun": "All", "fieldValidation": "Strict"},
        )
        tasks.access(ctx, "storage_migration")
        request(api, "PUT", BASE + "/" + payload["name"], body=desired, params={"fieldValidation": "Strict"})
    migration_live(ctx, payload)
    tasks.progress(ctx, prepared=True)
    return {"target_version": payload["target"]}


def rewrite_instances(ctx):
    api, payload, _ = tasks.access(ctx, "storage_migration")
    api, definition, _ = migration_live(ctx, payload)
    if payload["backend"] == "native":
        return native_rewrite(ctx, payload)
    if manual_topology(api) != payload["topology"]:
        raise ConflictError("Control-plane topology changed")
    bound_rv = definition["metadata"]["resourceVersion"]
    written = 0
    deleted = 0
    for collection, objects in pages(
        api, definition, payload["target"], MAX_OBJECTS, guard=lambda: tasks.access(ctx, "storage_migration")
    ):
        for obj in objects:
            api, current_def, _ = migration_live(ctx, payload)
            if current_def["metadata"]["resourceVersion"] != bound_rv:
                raise ConflictError("Definition changed during the scan; retry the rewrite with a fresh snapshot")
            path = instance_path(collection, obj)
            for attempt in range(3):
                status, _data = api.request("GET", path)
                if status == 404:
                    deleted += 1
                    break
                fresh = request(api, "GET", path)
                if fresh["metadata"]["uid"] != obj["metadata"]["uid"]:
                    raise ConflictError("Instance identity changed during rewrite")
                desired = unchanged_dry_run(api, path, fresh)
                tasks.access(ctx, "storage_migration")
                try:
                    actual = request(api, "PUT", path, body=desired, params={"fieldValidation": "Strict"})
                except ConflictError:
                    if attempt == 2:
                        raise
                    continue
                if actual["metadata"]["uid"] != obj["metadata"]["uid"] or content(actual) != content(fresh):
                    raise ConflictError("Admission changed rewritten content; old stored-version metadata is retained")
                written += 1
                break
            tasks.progress(ctx, rewritten=written, deleted=deleted)
    api, current_def, _ = migration_live(ctx, payload)
    if current_def["metadata"]["resourceVersion"] != bound_rv:
        raise ConflictError("Definition changed before scan certification; retry rewrite")
    if manual_topology(api) != payload["topology"]:
        raise ConflictError("Control-plane topology changed before certification")
    tasks.progress(ctx, scan_complete=True, scan_definition_rv=bound_rv, rewritten=written)
    return {"rewritten": written, "deleted_during_scan": deleted}


def certified_result(payload, spec):
    return {
        "status": "Migrated",
        "backend": "native",
        "target_version": payload["target"],
        "native_migration_uid": spec.get("progress", {}).get("native_uid"),
        "reviewed_instances": spec.get("summary", {}).get("reviewed_instances", 0),
    }


def finalize_migration(ctx):
    api, payload, spec = tasks.access(ctx, "storage_migration")
    api, live, spec = migration_live(ctx, payload)
    progress = spec.get("progress", {})
    if progress.get("certified"):
        result = certified_result(payload, spec)
        ctx.set_result(result)
        return result
    if payload["backend"] == "native":
        native = request(api, "GET", NATIVE + "/" + progress.get("native_name", "missing"))
        if (
            not progress.get("native_succeeded")
            or native["metadata"]["uid"] != progress.get("native_uid")
            or native.get("spec", {}).get("resource")
            != {
                "group": payload["original"]["spec"]["group"],
                "resource": payload["original"]["spec"]["names"]["plural"],
            }
            or native.get("metadata", {}).get("labels", {}).get("kplatform.io/migration-review")
            != str(ctx.params.review_id)
            or not any(
                c.get("type") == "Succeeded" and c.get("status") == "True"
                for c in native.get("status", {}).get("conditions", [])
            )
        ):
            raise InvalidStateError("Native migration has not certified completion")
    else:
        if (
            manual_topology(api) != payload["topology"]
            or not progress.get("scan_complete")
            or live["metadata"]["resourceVersion"] != progress.get("scan_definition_rv")
        ):
            raise ConflictError("A stable complete pass is required even for an uncertified rewrite")
        result = {
            "status": "RewrittenUncertified",
            "rewritten": progress.get("rewritten", 0),
            "stored_versions_retained": True,
        }
        tasks.progress(ctx, rewrite_only_complete=True)
        ctx.set_result(result)
        return result
    if progress.get("certifying") and (live.get("status") or {}).get("storedVersions") == [payload["target"]]:
        tasks.progress(ctx, certified=True)
        result = certified_result(payload, spec)
        ctx.set_result(result)
        return result
    if not progress.get("scan_complete") or (
        payload["backend"] != "native" and live["metadata"]["resourceVersion"] != progress.get("scan_definition_rv")
    ):
        raise ConflictError(
            "A complete unchanged-definition pass is required before clearing stored versions; "
            "create a new review after definition changes"
        )
    status = copy.deepcopy(live.get("status") or {})
    status["storedVersions"] = [payload["target"]]
    body = {
        "apiVersion": live["apiVersion"],
        "kind": live["kind"],
        "metadata": copy.deepcopy(live["metadata"]),
        "status": status,
    }
    body["metadata"].pop("managedFields", None)
    tasks.access(ctx, "storage_migration")
    tasks.progress(ctx, certifying=True)
    request(api, "PUT", BASE + "/" + payload["name"] + "/status", body=body)
    tasks.progress(ctx, certified=True)
    result = certified_result(payload, spec)
    ctx.set_result(result)
    return result


def check_service(api, conversion):
    if conversion.get("strategy") != "Webhook":
        return None
    config = conversion.get("webhook", {}).get("clientConfig", {})
    service = config.get("service")
    if not service or config.get("url") or not config.get("caBundle"):
        raise InvalidStateError("A CA-verified in-cluster conversion Service is required")
    namespace = service.get("namespace", "")
    name = service.get("name", "")
    if not re.fullmatch(DNS, namespace) or not re.fullmatch(DNS, name):
        raise InvalidStateError("Invalid conversion Service identity")
    live = request(api, "GET", f"/api/v1/namespaces/{namespace}/services/{name}")
    if live.get("spec", {}).get("type") == "ExternalName" or live["metadata"].get("deletionTimestamp"):
        raise InvalidStateError("Conversion Service must be active and in-cluster")
    port = service.get("port", 443)
    ports = [
        p for p in live.get("spec", {}).get("ports", []) if p.get("port") == port and p.get("protocol", "TCP") == "TCP"
    ]
    if not ports:
        raise InvalidStateError("Conversion Service TCP port is not available")
    slices = request(
        api,
        "GET",
        f"/apis/discovery.k8s.io/v1/namespaces/{namespace}/endpointslices",
        params={"labelSelector": "kubernetes.io/service-name=" + name, "limit": 1000},
    )
    if (slices.get("metadata") or {}).get("continue"):
        raise InvalidStateError("Conversion endpoints inventory is incomplete")
    ready = False
    for item in slices.get("items", []):
        owners = item.get("metadata", {}).get("ownerReferences") or []
        if owners and not any(o.get("uid") == live["metadata"]["uid"] for o in owners):
            continue
        if not any(
            p.get("name") == ports[0].get("name") and p.get("port") and p.get("protocol", "TCP") == "TCP"
            for p in item.get("ports", [])
        ):
            continue
        if any(e.get("conditions", {}).get("ready") is True and e.get("addresses") for e in item.get("endpoints", [])):
            ready = True
    if not ready:
        raise InvalidStateError("Conversion Service has no Ready EndpointSlice backend")
    return live["metadata"]["uid"]


def conversion_config(body):
    if body.strategy == "None":
        return {"strategy": "None"}
    if not body.service_name:
        raise ValidationFailedError("Conversion Service name is required")
    if any(segment in {".", ".."} for segment in body.path.split("/")):
        raise ValidationFailedError("Conversion path must not contain traversal segments")
    pem = body.ca_pem.get_secret_value()
    try:
        if len(pem.encode()) > 65536 or not re.fullmatch(
            r"(?:\s*-----BEGIN CERTIFICATE-----[A-Za-z0-9+/=\r\n]+-----END CERTIFICATE-----\s*)+", pem
        ):
            raise ValueError()
        certs = x509.load_pem_x509_certificates(pem.encode())
        if not certs or any(
            c.not_valid_after_utc <= datetime.now(UTC) or c.not_valid_before_utc > datetime.now(UTC) for c in certs
        ):
            raise ValueError()
    except ValueError:
        raise ValidationFailedError(
            "Provide a currently valid certificate-only PEM CA bundle; excerpts withheld"
        ) from None
    return {
        "strategy": "Webhook",
        "webhook": {
            "conversionReviewVersions": ["v1"],
            "clientConfig": {
                "service": {
                    "namespace": body.service_namespace,
                    "name": body.service_name,
                    "port": body.port,
                    "path": body.path,
                },
                "caBundle": base64.b64encode(pem.encode()).decode(),
            },
        },
    }


def probe_instances(api, live, guard=lambda: None):
    served = [v["name"] for v in live["spec"]["versions"] if v.get("served")]
    count = 0
    for collection, objects in pages(api, live, storage_version(live), MAX_PROBES, guard=guard):
        for obj in objects:
            for version in served:
                guard()
                converted_collection = collection.replace("/" + storage_version(live) + "/", "/" + version + "/")
                converted = request(api, "GET", instance_path(converted_collection, obj))
                if (
                    converted["metadata"]["uid"] != obj["metadata"]["uid"]
                    or converted.get("apiVersion") != live["spec"]["group"] + "/" + version
                ):
                    raise ConflictError("Conversion changed instance identity or version")
                unchanged_dry_run(api, instance_path(converted_collection, obj), converted)
            count += 1
    return count


def review_conversion(service, principal, cluster_id, body):
    cluster, api, live = baseline(service, principal, cluster_id, body)
    before = live["spec"].get("conversion") or {"strategy": "None"}
    config = before if body.action == "probe" else conversion_config(body)
    if body.action == "probe" and config.get("strategy") != "Webhook":
        raise InvalidStateError("No conversion webhook is configured")
    if (
        config["strategy"] == "None"
        and len({json.dumps(v.get("schema"), sort_keys=True) for v in live["spec"]["versions"]}) > 1
    ):
        raise InvalidStateError("None conversion requires identical version schemas")
    service_uid = check_service(api, config)
    count = 0
    for _, objects in pages(api, live, storage_version(live), MAX_PROBES):
        count += len(objects)
    desired = copy.deepcopy(live)
    desired["spec"]["conversion"] = config
    if body.action == "configure":
        request(api, "PUT", BASE + "/" + body.name, body=desired, params={"dryRun": "All", "fieldValidation": "Strict"})
    return tasks.persist(
        service,
        principal,
        cluster,
        "conversion_webhook",
        body.name,
        {
            "name": body.name,
            "uid": body.expected_uid,
            "before_hash": body.expected_hash,
            "original": live,
            "config": config,
            "service_uid": service_uid,
            "action": body.action,
        },
        {
            "definition": body.name,
            "strategy": config["strategy"],
            "action": body.action,
            "reviewed_instances": count,
            "notice": "Service readiness is separate from conversion. Instances are probed through served APIs; "
            "failed configuration probes trigger conditional rollback.",
        },
    )


DISCOVERY_WAIT_SECONDS = 30
DISCOVERY_POLL_SECONDS = 0.25


def replace_spec(api, path, uid, expected, desired, cluster_id, marker=None):
    for attempt in range(3):
        live = request(api, "GET", path)
        check_ownership(live, cluster_id)
        if live["metadata"]["uid"] != uid or live["spec"] != expected:
            raise ConflictError("Concurrent definition changes prevent the conditional specification update")
        updated = copy.deepcopy(live)
        updated["spec"] = copy.deepcopy(desired)
        updated["metadata"].pop("managedFields", None)
        if marker:
            updated["metadata"].setdefault("annotations", {})[CONVERSION_MARKER] = marker
        try:
            return request(api, "PUT", path, body=updated, params={"fieldValidation": "Strict"})
        except ConflictError:
            if attempt == 2:
                raise
    raise ConflictError("Definition is changing too quickly")


def wait_discovery(api, definition, spec, alias, present, guard=lambda: None):
    deadline = time.monotonic() + DISCOVERY_WAIT_SECONDS
    while True:
        guard()
        observed = True
        for version in spec["versions"]:
            if not version.get("served"):
                continue
            listing = request(api, "GET", "/apis/" + spec["group"] + "/" + version["name"])
            resource = next((r for r in listing.get("resources", []) if r.get("name") == spec["names"]["plural"]), None)
            if resource is None or ((alias in (resource.get("shortNames") or [])) != present):
                observed = False
        if observed:
            return
        if time.monotonic() >= deadline:
            raise InvalidStateError("API discovery has not observed this conversion configuration")
        time.sleep(DISCOVERY_POLL_SECONDS)


def configure_conversion(ctx):
    api, payload, state = tasks.access(ctx, "conversion_webhook")
    path = BASE + "/" + payload["name"]
    live = request(api, "GET", path)
    check_ownership(live, ctx.params.cluster_id)
    if live["metadata"]["uid"] != payload["uid"]:
        raise ConflictError("Definition identity changed")
    progress = state.get("progress", {})
    if progress.get("complete"):
        ctx.set_result(progress["complete"])
        return progress["complete"]
    if progress.get("rolled_back"):
        raise InvalidStateError("Previous probe rolled back; create a fresh review")
    config = payload["config"]
    if check_service(api, config) != payload["service_uid"]:
        raise ConflictError("Conversion Service was replaced")
    original = payload["original"]["spec"]
    final = copy.deepcopy(original)
    final["conversion"] = config
    alias = "kp" + ctx.params.review_id.hex[:12]
    probing = copy.deepcopy(final)
    probing["names"]["shortNames"] = [*(original["names"].get("shortNames") or []), alias]
    changed = payload["action"] == "configure"
    already = (live["metadata"].get("annotations") or {}).get(CONVERSION_MARKER) == str(ctx.params.review_id)
    if not already and fingerprint(live) != payload["before_hash"]:
        raise ConflictError("Definition changed after review")
    if already and live["spec"] not in (probing, final):
        raise ConflictError("Configured definition changed")
    if changed and already and live["spec"] == final and not progress.get("probes_passed"):
        raise ConflictError("Probe observation was lost; create a fresh review")
    expected = probing if changed else original

    def guard():
        tasks.access(ctx, "conversion_webhook")
        current = request(api, "GET", path)
        check_ownership(current, ctx.params.cluster_id)
        if current["metadata"]["uid"] != payload["uid"] or current["spec"] != expected:
            raise ConflictError("Definition changed during conversion probe")
        if check_service(api, config) != payload["service_uid"]:
            raise ConflictError("Conversion Service changed")

    try:
        if changed and not already:
            tasks.progress(ctx, configure_started=True)
            tasks.access(ctx, "conversion_webhook")
            live = replace_spec(
                api, path, payload["uid"], original, probing, ctx.params.cluster_id, str(ctx.params.review_id)
            )
        if changed and progress.get("probes_passed"):
            count = progress["probes_passed"]["instances"]
        else:
            tasks.progress(ctx, configured=changed, probe_alias=alias if changed else None)
            if changed:
                wait_discovery(api, payload["name"], probing, alias, True, guard)
            guard()
            count = probe_instances(api, live, guard)
            guard()
            tasks.progress(ctx, probes_passed={"instances": count, "at": datetime.now(UTC).isoformat()})
        if changed:
            current = request(api, "GET", path)
            if current["spec"] == probing:
                tasks.access(ctx, "conversion_webhook")
                replace_spec(api, path, payload["uid"], probing, final, ctx.params.cluster_id)
            elif current["spec"] != final:
                raise ConflictError("Definition changed before probe alias cleanup")
            wait_discovery(api, payload["name"], final, alias, False, lambda: tasks.access(ctx, "conversion_webhook"))
    except Exception:
        if changed:
            current = request(api, "GET", path)
            if current["metadata"]["uid"] == payload["uid"] and current["spec"] in (probing, final):
                replace_spec(api, path, payload["uid"], current["spec"], original, ctx.params.cluster_id)
                tasks.progress(ctx, rolled_back=True)
                try:
                    wait_discovery(api, payload["name"], original, alias, False)
                except PlatformError:
                    ctx.warning("Previous spec restored; discovery observation is still pending")
                ctx.info("Conversion probe failed; previous specification conditionally restored")
            else:
                ctx.warning("Conversion probe failed; concurrent definition changes prevented rollback")
        raise
    result = {
        "status": "Verified" if count else "ConfiguredWithoutInstanceProbe",
        "probed_instances": count,
        "strategy": config["strategy"],
        "service_ready": payload["service_uid"] is not None,
    }
    tasks.progress(ctx, complete=result)
    ctx.set_result(result)
    return result


def fixed_plan(steps):
    def plan(_params: tasks.ReviewedParams) -> list[StepDefinition]:
        return steps

    return plan


MIGRATION_STEPS = [
    StepDefinition("prepare", "Prepare the reviewed storage operation", tasks.safe_step(prepare_migration)),
    StepDefinition("rewrite", "Rewrite current instances", tasks.safe_step(rewrite_instances)),
    StepDefinition("certify", "Report certification or retained history", tasks.safe_step(finalize_migration)),
]

for operation_type, steps in [
    ("cluster.crd.rewrite", MIGRATION_STEPS),
    (
        "cluster.crd.migrate",
        [
            StepDefinition("prepare", "Select the reviewed storage version", tasks.safe_step(prepare_migration)),
            StepDefinition("rewrite", "Rewrite and verify all current instances", tasks.safe_step(rewrite_instances)),
            StepDefinition("certify", "Certify stored-version metadata", tasks.safe_step(finalize_migration)),
        ],
    ),
    (
        "cluster.crd.conversion",
        [
            StepDefinition(
                "configure",
                "Configure and probe conversion; roll back on failure",
                tasks.safe_step(configure_conversion),
            )
        ],
    ),
]:
    workflow_registry.register(
        WorkflowDefinition(
            type=operation_type,
            title="CRD "
            + (
                "uncertified storage rewrite"
                if operation_type.endswith("rewrite")
                else "storage migration"
                if operation_type.endswith("migrate")
                else "conversion webhook"
            ),
            description="Execute the approved CRD operation with checkpoints and scoped access.",
            params_model=tasks.ReviewedParams,
            destructive=True,
            target=lambda p: OperationTarget("cluster", str(p.cluster_id), p.cluster_name),
            plan=fixed_plan(steps),
        )
    )
