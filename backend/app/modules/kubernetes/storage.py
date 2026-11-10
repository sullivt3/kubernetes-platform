"""Pinned NFS CSI add-on lifecycle, with ownership and volume-use safeguards."""

from __future__ import annotations

import hashlib
import time
import urllib.request
from typing import Any

from pydantic import BaseModel, Field, model_validator

from app.core.errors import ConflictError
from app.modules.kubernetes import apply as kapply
from app.modules.kubernetes import client as kc
from app.modules.kubernetes.phase6 import workflow_api
from app.modules.kubernetes.workflows import ConnectParams, _call
from app.orchestration.context import StepContext
from app.orchestration.workflow import (
    OperationTarget,
    RetryableStepError,
    StepDefinition,
    StepFailedError,
    WorkflowDefinition,
    workflow_registry,
)

NFS_VERSION = "v4.13.6"
NFS_DRIVER = "nfs.csi.k8s.io"
NFS_INSTALL = "cluster.storage.nfs.install"
NFS_REMOVE = "cluster.storage.nfs.remove"
LABEL = "kubernetes-platform.io/addon"
LABEL_VALUE = "nfs-csi"
LABELS = {LABEL: LABEL_VALUE, "app.kubernetes.io/managed-by": "kubernetes-platform"}
BASE_URL = f"https://raw.githubusercontent.com/kubernetes-csi/csi-driver-nfs/{NFS_VERSION}/deploy/{NFS_VERSION}"
MANIFESTS = {
    "rbac-csi-nfs.yaml": "312de689d07627330d8280e50d7b51357e0b5f5ca0d8aa75794aec460ee4cff0",
    "csi-nfs-driverinfo.yaml": "fa2bba4674821b3317d1e016f3c9d54abe04fcdc6fed3fce9e58994c8311c4a0",
    "csi-nfs-controller.yaml": "706bf8e69ac03e810cec806bed11050850588cab615aa65d12a6101cecfd3871",
    "csi-nfs-node.yaml": "26a9ee1c9de6608598ac9083927a922a77323f8933ba2834d61a665b6d23f692",
}
SC_PATH = "/apis/storage.k8s.io/v1/storageclasses"
DRIVER_PATH = f"/apis/storage.k8s.io/v1/csidrivers/{NFS_DRIVER}"
CONTROLLER_PATH = "/apis/apps/v1/namespaces/kube-system/deployments/csi-nfs-controller"
NODE_PATH = "/apis/apps/v1/namespaces/kube-system/daemonsets/csi-nfs-node"
# Removal uses a fixed resource inventory; it does not need an upstream download.
RESOURCE_PATHS = [
    "/api/v1/namespaces/kube-system/serviceaccounts/csi-nfs-controller-sa",
    "/api/v1/namespaces/kube-system/serviceaccounts/csi-nfs-node-sa",
    "/apis/rbac.authorization.k8s.io/v1/clusterroles/nfs-external-provisioner-role",
    "/apis/rbac.authorization.k8s.io/v1/clusterrolebindings/nfs-csi-provisioner-binding",
    "/apis/rbac.authorization.k8s.io/v1/clusterroles/nfs-external-resizer-role",
    "/apis/rbac.authorization.k8s.io/v1/clusterrolebindings/nfs-csi-resizer-role",
    DRIVER_PATH,
    CONTROLLER_PATH,
    NODE_PATH,
]


class NfsInstall(BaseModel):
    storage_class: str | None = Field(
        default=None, min_length=1, max_length=63, pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"
    )
    server: str | None = Field(default=None, min_length=1, max_length=253, pattern=r"^[A-Za-z0-9][A-Za-z0-9.:-]*$")
    share: str | None = Field(default=None, max_length=1024, pattern=r"^/[A-Za-z0-9_./-]*$")
    make_default: bool = False

    @model_validator(mode="after")
    def validate_storage_class(self) -> NfsInstall:
        if self.storage_class and not (self.server and self.share):
            raise ValueError("An NFS server and export path are required for a StorageClass")
        if not self.storage_class and (self.make_default or self.server or self.share):
            raise ValueError("Select a StorageClass name to configure NFS storage")
        return self


class InstallParams(ConnectParams, NfsInstall):
    pass


def managed(obj: dict[str, Any]) -> bool:
    labels = obj.get("metadata", {}).get("labels") or {}
    return all(labels.get(key) == value for key, value in LABELS.items())


def optional(api: kc.ClusterAPI, path: str) -> dict[str, Any] | None:
    try:
        return kc.get_json(api, path)
    except kc.NotFoundInClusterError:
        return None


def download_manifests() -> list[dict[str, Any]]:
    objects = []
    for filename, expected in MANIFESTS.items():
        try:
            with urllib.request.urlopen(f"{BASE_URL}/{filename}", timeout=30) as response:
                data = response.read(2 * 1024 * 1024 + 1)
        except OSError as exc:
            raise RetryableStepError(f"Could not download the pinned NFS CSI manifest {filename}: {exc}") from exc
        if hashlib.sha256(data).hexdigest() != expected:
            raise StepFailedError(
                f"Integrity verification failed for NFS CSI manifest {filename}", code="manifest_integrity_failed"
            )
        objects.extend(kapply.parse_manifest(data.decode()))
    for obj in objects:
        meta = obj.setdefault("metadata", {})
        meta.setdefault("labels", {}).update(LABELS)
        meta.setdefault("annotations", {})["kubernetes-platform.io/addon-version"] = NFS_VERSION
    return objects


def storage_class(p: NfsInstall) -> dict[str, Any]:
    return {
        "apiVersion": "storage.k8s.io/v1",
        "kind": "StorageClass",
        "metadata": {
            "name": p.storage_class,
            "labels": LABELS.copy(),
            "annotations": {"storageclass.kubernetes.io/is-default-class": str(p.make_default).lower()},
        },
        "provisioner": NFS_DRIVER,
        "parameters": {"server": p.server, "share": p.share, "onDelete": "retain"},
        "reclaimPolicy": "Retain",
        "volumeBindingMode": "Immediate",
        "allowVolumeExpansion": True,
        "mountOptions": ["nfsvers=4.1"],
    }


def assert_owned(obj: dict[str, Any] | None, path: str) -> None:
    if obj is not None and not managed(obj):
        raise ConflictError(f"{path} exists and is not managed by this platform's NFS add-on; refusing to modify it")


def check_default(api: kc.ClusterAPI, p: NfsInstall) -> None:
    if p.make_default:
        classes = inventory(api, SC_PATH)
        others = [
            c["metadata"]["name"]
            for c in classes
            if c["metadata"]["name"] != p.storage_class
            and any(
                (c["metadata"].get("annotations") or {}).get(a) == "true"
                for a in (
                    "storageclass.kubernetes.io/is-default-class",
                    "storageclass.beta.kubernetes.io/is-default-class",
                )
            )
        ]
        if others:
            raise ConflictError(
                "Another default StorageClass already exists: "
                + ", ".join(others)
                + ". Clear its default annotation before selecting a new default."
            )


def apply_driver(ctx: StepContext, api: kc.ClusterAPI, p: InstallParams) -> dict[str, Any]:
    nodes, _ = kc.list_items(api, "/api/v1/nodes")
    if not any(n.get("metadata", {}).get("labels", {}).get("kubernetes.io/os") == "linux" for n in nodes):
        raise StepFailedError(
            "NFS CSI requires Linux nodes with the standard /var/lib/kubelet directory", code="nfs_linux_required"
        )
    objects = download_manifests()
    if p.storage_class:
        objects.append(storage_class(p))
    discovery = kapply.Discovery(api)
    paths = [(discovery.object_path(obj), obj) for obj in objects]
    for path, _obj in paths:
        assert_owned(optional(api, path), path)
    check_default(api, p)
    for path, obj in paths:
        ctx.check_cancelled()
        # Check ownership again before writing; use create-only or optimistic concurrency.
        current = optional(api, path)
        assert_owned(current, path)
        if obj["kind"] == "StorageClass":
            check_default(api, p)
        if current is None:
            # POST fails if another owner creates this resource after preflight.
            kc.send_json(
                api, "POST", path.rsplit("/", 1)[0], obj, params={"fieldManager": "kubernetes-platform-nfs-csi"}
            )
        else:
            if current["metadata"].get("resourceVersion"):
                obj["metadata"]["resourceVersion"] = current["metadata"]["resourceVersion"]
            kc.send_json(
                api,
                "PATCH",
                path,
                obj,
                content_type=kc.MERGE_PATCH,
                params={"fieldManager": "kubernetes-platform-nfs-csi"},
            )
        ctx.info(f"Applied {obj['kind']}/{obj['metadata']['name']}")
    return {"version": NFS_VERSION, "objects": len(objects)}


def readiness(controller: dict[str, Any] | None, node: dict[str, Any] | None) -> dict[str, Any]:
    cs, ns = (controller or {}).get("status") or {}, (node or {}).get("status") or {}
    desired = int(ns.get("desiredNumberScheduled") or 0)
    controller_desired = ((controller or {}).get("spec") or {}).get("replicas", 1)
    controller_ready = bool(
        controller
        and cs.get("observedGeneration", 0) >= controller.get("metadata", {}).get("generation", 1)
        and cs.get("updatedReplicas", 0) >= controller_desired
        and cs.get("replicas", 0) == controller_desired
        and cs.get("availableReplicas", 0) >= controller_desired
        and controller_desired > 0
    )
    node_ready = bool(
        node
        and ns.get("observedGeneration", 0) >= node.get("metadata", {}).get("generation", 1)
        and desired > 0
        and ns.get("updatedNumberScheduled", 0) == desired
        and ns.get("numberReady", 0) == desired
    )
    return {
        "ready": controller_ready and node_ready,
        "controller_ready": controller_ready,
        "nodes_ready": ns.get("numberReady", 0),
        "nodes_desired": desired,
    }


def status(api: kc.ClusterAPI) -> dict[str, Any]:
    resources = {path: optional(api, path) for path in RESOURCE_PATHS}
    driver = resources[DRIVER_PATH]
    controller, node = resources[CONTROLLER_PATH], resources[NODE_PATH]
    existing = [obj for obj in resources.values() if obj is not None]
    classes, _ = kc.list_items(api, SC_PATH)
    return {
        "driver": NFS_DRIVER,
        "version": NFS_VERSION,
        "maturity": "preview",
        "verified_live": False,
        "installed": driver is not None,
        "managed": bool(existing) and all(managed(obj) for obj in existing),
        **readiness(controller, node),
        "storage_classes": [
            {"name": c["metadata"]["name"], "managed": managed(c)}
            for c in classes
            if c.get("provisioner") == NFS_DRIVER
        ],
    }


def wait_driver(ctx: StepContext, api: kc.ClusterAPI) -> dict[str, Any]:
    deadline = time.monotonic() + 300
    while True:
        ctx.check_cancelled()
        result = readiness(optional(api, CONTROLLER_PATH), optional(api, NODE_PATH))
        if result["ready"]:
            ctx.set_result({"driver": NFS_DRIVER, "version": NFS_VERSION, **result})
            return result
        if time.monotonic() >= deadline:
            raise StepFailedError(
                "NFS CSI was applied but its controller and Linux node DaemonSet did not become ready within "
                "five minutes. Inspect their events and logs, then retry the operation.",
                code="nfs_not_ready",
            )
        ctx.set_progress(min(0.95, result["nodes_ready"] / max(result["nodes_desired"], 1)))
        time.sleep(3)


def inventory(api: kc.ClusterAPI, path: str) -> list[dict[str, Any]]:
    objects, truncated = kc.list_items(api, path)
    if truncated:
        raise ConflictError("Cannot establish a complete storage inventory; refusing to modify the NFS add-on")
    return objects


def removal_inventory(
    api: kc.ClusterAPI, remembered_classes: set[str] | None = None
) -> list[tuple[str, dict[str, Any]]]:
    classes = inventory(api, SC_PATH)
    classes = [c for c in classes if c.get("provisioner") == NFS_DRIVER]
    class_names = {c["metadata"]["name"] for c in classes} | (remembered_classes or set())
    volumes = inventory(api, "/api/v1/persistentvolumes")
    claims = inventory(api, "/api/v1/persistentvolumeclaims")
    attachments = inventory(api, "/apis/storage.k8s.io/v1/volumeattachments")
    # Include Pending claims and Released volumes: either may still need this provisioner.
    uses = [
        v["metadata"]["name"]
        for v in volumes
        if (v.get("spec", {}).get("csi") or {}).get("driver") == NFS_DRIVER
        or v.get("spec", {}).get("storageClassName") in class_names
    ]
    uses += [
        f"{c['metadata'].get('namespace')}/{c['metadata']['name']}"
        for c in claims
        if c.get("spec", {}).get("storageClassName") in class_names
        or (c["metadata"].get("annotations") or {}).get("volume.kubernetes.io/storage-provisioner") == NFS_DRIVER
        or (
            c.get("spec", {}).get("storageClassName") is None
            and any(
                any(
                    (s["metadata"].get("annotations") or {}).get(key) == "true"
                    for key in (
                        "storageclass.kubernetes.io/is-default-class",
                        "storageclass.beta.kubernetes.io/is-default-class",
                    )
                )
                for s in classes
            )
        )
    ]
    uses += [a["metadata"]["name"] for a in attachments if a.get("spec", {}).get("attacher") == NFS_DRIVER]
    if uses:
        raise ConflictError("NFS CSI is still referenced by volumes, claims or attachments: " + ", ".join(uses[:20]))
    resources = [(f"{SC_PATH}/{c['metadata']['name']}", c) for c in classes]
    resources += [(path, obj) for path in reversed(RESOURCE_PATHS) if (obj := optional(api, path)) is not None]
    for path, obj in resources:
        assert_owned(obj, path)
    return resources


def remove_driver(ctx: StepContext, api: kc.ClusterAPI) -> dict[str, Any]:
    resources = removal_inventory(api)
    class_names = {obj["metadata"]["name"] for _path, obj in resources if obj["kind"] == "StorageClass"}
    for path, obj in resources:
        ctx.check_cancelled()
        removal_inventory(api, class_names)  # Refuse removal if a claim appeared since preflight.
        current = optional(api, path)
        if current is None:
            continue
        assert_owned(current, path)
        if current["metadata"]["uid"] != obj["metadata"]["uid"]:
            raise ConflictError(f"{path} was replaced during removal; retry after reviewing the resource")
        kc.send_json(
            api, "DELETE", path, {"propagationPolicy": "Background", "preconditions": {"uid": obj["metadata"]["uid"]}}
        )
        ctx.info(f"Deleted {path}")
    result = {"driver": NFS_DRIVER, "removed": len(resources), "volumes_deleted": 0}
    ctx.set_result(result)
    return result


def step_install(ctx: StepContext) -> dict[str, Any]:
    p: InstallParams = ctx.params  # type: ignore[assignment]
    return _call(lambda: apply_driver(ctx, workflow_api(ctx, p.cluster_id), p))


def step_wait(ctx: StepContext) -> dict[str, Any]:
    p: InstallParams = ctx.params  # type: ignore[assignment]
    return _call(lambda: wait_driver(ctx, workflow_api(ctx, p.cluster_id)))


def step_remove(ctx: StepContext) -> dict[str, Any]:
    p: ConnectParams = ctx.params  # type: ignore[assignment]
    return _call(lambda: remove_driver(ctx, workflow_api(ctx, p.cluster_id)))


workflow_registry.register(
    WorkflowDefinition(
        type=NFS_INSTALL,
        title="Install NFS CSI driver",
        description="Apply the pinned NFS CSI driver, optionally configure storage, and wait for readiness.",
        params_model=InstallParams,
        plan=lambda _p: [
            StepDefinition("apply", "Install NFS CSI and configure storage", step_install, max_attempts=2),
            StepDefinition("ready", "Wait for controller and node plugins", step_wait),
        ],
        target=lambda p: OperationTarget(type="cluster", id=str(p.cluster_id), name=p.cluster_name),
        title_for=lambda p: f"Install NFS CSI on {p.cluster_name}",
    )
)
workflow_registry.register(
    WorkflowDefinition(
        type=NFS_REMOVE,
        title="Remove NFS CSI driver",
        description="Refuse removal while storage is in use; delete only platform-managed driver resources.",
        params_model=ConnectParams,
        destructive=True,
        plan=lambda _p: [
            StepDefinition("remove", "Check storage usage and remove managed NFS CSI resources", step_remove)
        ],
        target=lambda p: OperationTarget(type="cluster", id=str(p.cluster_id), name=p.cluster_name),
        title_for=lambda p: f"Remove NFS CSI from {p.cluster_name}",
    )
)
