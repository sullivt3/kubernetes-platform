"""Phase 6: real workflow execution against the replaceable Kubernetes transport."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.errors import ConflictError
from app.modules.kubernetes import client as kc
from app.modules.kubernetes import dns, storage
from app.orchestration.workflow import OperationCancelledError, StepFailedError
from tests.fake_kube import kubeconfig_text
from tests.fake_store import RESOURCES, FakeStore

API = "/api/v1"


class AddonStore(FakeStore):
    """Simulate controller reconciliation and a completed DNS utility container."""

    dns_exit_code = 0
    dns_pending = False

    def request(self, method: str, path: str, **kwargs: Any) -> tuple[int, bytes]:
        result = super().request(method, path, **kwargs)
        applied_path = ""
        if method in ("PATCH", "POST") and result[0] < 400:
            applied_path = path if method == "PATCH" else self.path_for(json.loads(result[1]))
        if (
            method in ("PATCH", "POST")
            and applied_path in (storage.CONTROLLER_PATH, storage.NODE_PATH)
            and result[0] < 400
        ):
            obj = self.objects[applied_path]
            obj["status"] = (
                {
                    "observedGeneration": obj["metadata"]["generation"],
                    "updatedReplicas": 1,
                    "availableReplicas": 1,
                    "replicas": 1,
                }
                if applied_path == storage.CONTROLLER_PATH
                else {
                    "observedGeneration": obj["metadata"]["generation"],
                    "desiredNumberScheduled": 1,
                    "updatedNumberScheduled": 1,
                    "numberReady": 1,
                }
            )
        if method == "POST" and path.endswith("/jobs") and result[0] < 400:
            job = json.loads(result[1])
            namespace = job["metadata"]["namespace"]
            self.put(
                {
                    "apiVersion": "v1",
                    "kind": "Pod",
                    "metadata": {
                        "name": "dns-pod",
                        "namespace": namespace,
                        "labels": job["metadata"]["labels"],
                        "ownerReferences": [{"uid": job["metadata"]["uid"]}],
                    },
                    "status": {}
                    if self.dns_pending
                    else {
                        "containerStatuses": [
                            {"name": "lookup", "state": {"terminated": {"exitCode": self.dns_exit_code}}}
                        ]
                    },
                }
            )
            self.logs[f"/api/v1/namespaces/{namespace}/pods/dns-pod/log"] = (
                "DNS answer" if self.dns_exit_code == 0 else "NXDOMAIN"
            )
        return result


@pytest.fixture
def addon_store() -> Any:
    fake = AddonStore()
    fake.put_namespace("kube-system")
    fake.put(
        {"apiVersion": "v1", "kind": "Node", "metadata": {"name": "linux", "labels": {"kubernetes.io/os": "linux"}}}
    )
    kc.set_client_factory(fake.factory)
    yield fake
    kc.set_client_factory(None)


@pytest.fixture
def cluster_id(client: TestClient, addon_store: AddonStore) -> str:
    response = client.post(f"{API}/clusters/import", json={"name": "phase6", "kubeconfig": kubeconfig_text()})
    assert response.status_code == 202
    return response.json()["cluster_id"]


@pytest.fixture
def manifests(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    objects = []
    for path in storage.RESOURCE_PATHS:
        base, plural, name = path.rsplit("/", 2)
        api_version = "v1" if path.startswith("/api/") else "/".join(path.split("/")[2:4])
        kind = next(kind for resource, kind, _ in RESOURCES[api_version] if resource == plural)
        obj = {"apiVersion": api_version, "kind": kind, "metadata": {"name": name, "labels": storage.LABELS.copy()}}
        if "/namespaces/" in base:
            obj["metadata"]["namespace"] = "kube-system"
        objects.append(obj)
    monkeypatch.setattr(storage, "download_manifests", lambda: copy.deepcopy(objects))
    return objects


def test_nfs_install_reconcile_and_guarded_removal(
    client: TestClient,
    addon_store: AddonStore,
    cluster_id: str,
    manifests: list[dict[str, Any]],
) -> None:
    path = f"{API}/clusters/{cluster_id}/storage/nfs"
    body = {"storage_class": "nfs", "server": "192.0.2.10", "share": "/export", "make_default": True}
    for _ in range(2):
        response = client.post(path, json=body)
        assert response.status_code == 202, response.text
        assert response.json()["status"] == "succeeded", response.text
    status = client.get(path).json()
    assert status["ready"] and status["managed"] and status["nodes_ready"] == 1
    sc = addon_store.objects[f"{storage.SC_PATH}/nfs"]
    assert sc["reclaimPolicy"] == "Retain" and sc["parameters"]["onDelete"] == "retain"
    assert all("force" not in params for method, _, params in addon_store.requests if method == "PATCH")
    assert client.delete(path).status_code == 428
    claim_path = "/api/v1/namespaces/default/persistentvolumeclaims/pending"
    addon_store.put(
        {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": "pending"},
            "spec": {"storageClassName": "nfs"},
        }
    )
    blocked = client.delete(path, params={"confirm": "phase6"}).json()
    assert blocked["status"] == "failed" and "still referenced" in blocked["error"]["message"]
    assert storage.DRIVER_PATH in addon_store.objects
    addon_store.objects.pop(claim_path)
    removed = client.delete(path, params={"confirm": "phase6"}).json()
    assert removed["status"] == "succeeded" and removed["result"]["volumes_deleted"] == 0
    assert storage.DRIVER_PATH not in addon_store.objects and f"{storage.SC_PATH}/nfs" not in addon_store.objects
    assert client.delete(path, params={"confirm": "phase6"}).json()["status"] == "succeeded"


def test_install_rejects_foreign_resources_before_writes(
    client: TestClient,
    addon_store: AddonStore,
    cluster_id: str,
    manifests: list[dict[str, Any]],
) -> None:
    addon_store.put({"apiVersion": "storage.k8s.io/v1", "kind": "CSIDriver", "metadata": {"name": storage.NFS_DRIVER}})
    start = len(addon_store.requests)
    op = client.post(f"{API}/clusters/{cluster_id}/storage/nfs", json={}).json()
    assert op["status"] == "failed" and "not managed" in op["error"]["message"]
    assert not any(method in ("PATCH", "DELETE") for method, _, _ in addon_store.requests[start:])


def test_install_rejects_competing_default(
    client: TestClient,
    addon_store: AddonStore,
    cluster_id: str,
    manifests: list[dict[str, Any]],
) -> None:
    addon_store.put(
        {
            "apiVersion": "storage.k8s.io/v1",
            "kind": "StorageClass",
            "metadata": {"name": "other", "annotations": {"storageclass.kubernetes.io/is-default-class": "true"}},
            "provisioner": "external.test",
        }
    )
    start = len(addon_store.requests)
    op = client.post(
        f"{API}/clusters/{cluster_id}/storage/nfs",
        json={"storage_class": "nfs", "server": "nfs.test", "share": "/export", "make_default": True},
    ).json()
    assert op["status"] == "failed" and "default StorageClass" in op["error"]["message"]
    assert not any(method == "PATCH" for method, _, _ in addon_store.requests[start:])


@pytest.mark.parametrize("kind", ["pv", "attachment", "foreign-class"])
def test_removal_checks_volume_and_resource_ownership(addon_store: AddonStore, kind: str) -> None:
    if kind == "pv":
        addon_store.put(
            {
                "apiVersion": "v1",
                "kind": "PersistentVolume",
                "metadata": {"name": "released"},
                "spec": {"csi": {"driver": storage.NFS_DRIVER}},
                "status": {"phase": "Released"},
            }
        )
    elif kind == "attachment":
        addon_store.put(
            {
                "apiVersion": "storage.k8s.io/v1",
                "kind": "VolumeAttachment",
                "metadata": {"name": "attached"},
                "spec": {"attacher": storage.NFS_DRIVER},
            }
        )
    else:
        addon_store.put(
            {
                "apiVersion": "storage.k8s.io/v1",
                "kind": "StorageClass",
                "metadata": {"name": "foreign"},
                "provisioner": storage.NFS_DRIVER,
            }
        )
    with pytest.raises(ConflictError):
        storage.removal_inventory(addon_store)
    assert not any(method == "DELETE" for method, _, _ in addon_store.requests)


def test_download_verifies_pinned_manifest_integrity(monkeypatch: pytest.MonkeyPatch) -> None:
    data = b"apiVersion: v1\nkind: ServiceAccount\nmetadata:\n  name: test\n"
    monkeypatch.setattr(storage, "MANIFESTS", {"test.yaml": hashlib.sha256(data).hexdigest()})
    monkeypatch.setattr(storage.urllib.request, "urlopen", lambda *_args, **_kwargs: io.BytesIO(data))
    assert storage.managed(storage.download_manifests()[0])
    monkeypatch.setattr(storage, "MANIFESTS", {"test.yaml": "0" * 64})
    with pytest.raises(StepFailedError, match="Integrity verification"):
        storage.download_manifests()


def test_readiness_does_not_accept_stale_rollout() -> None:
    controller = {
        "metadata": {"generation": 2},
        "status": {"observedGeneration": 1, "updatedReplicas": 1, "availableReplicas": 1, "replicas": 1},
    }
    node = {
        "metadata": {"generation": 1},
        "status": {"observedGeneration": 1, "desiredNumberScheduled": 1, "updatedNumberScheduled": 1, "numberReady": 1},
    }
    assert not storage.readiness(controller, node)["ready"]
    controller["status"]["observedGeneration"] = 2
    assert storage.readiness(controller, node)["ready"]
    node["status"]["desiredNumberScheduled"] = 0
    assert not storage.readiness(controller, node)["ready"]


@pytest.mark.parametrize("exit_code", [0, 1])
def test_dns_lookup_collects_actual_output_and_cleans_up(
    client: TestClient, addon_store: AddonStore, cluster_id: str, exit_code: int
) -> None:
    addon_store.dns_exit_code = exit_code
    op = client.post(
        f"{API}/clusters/{cluster_id}/dns/lookup", json={"hostname": "kubernetes.default.svc", "record_type": "A"}
    ).json()
    assert op["status"] == "succeeded", op
    assert op["result"]["resolved"] == (exit_code == 0)
    assert op["result"]["output"] == ("DNS answer" if exit_code == 0 else "NXDOMAIN")
    assert not any("/jobs/" in path for path in addon_store.objects)
    actions = client.get(f"{API}/audit-events").json()["items"]
    assert any(e["action"] == "operation.submit" for e in actions)


def test_dns_cancel_and_timeout_clean_up(addon_store: AddonStore, monkeypatch: pytest.MonkeyPatch) -> None:
    addon_store.dns_pending = True
    p = dns.DnsParams(cluster_id=uuid.uuid4(), cluster_name="test")

    def cancel() -> None:
        raise OperationCancelledError()

    ctx = SimpleNamespace(operation_id=uuid.uuid4(), check_cancelled=cancel, info=lambda _message: None)
    with pytest.raises(OperationCancelledError):
        dns.run_lookup(ctx, addon_store, p)
    assert not any("/jobs/" in path for path in addon_store.objects)
    ctx.operation_id = uuid.uuid4()
    ctx.check_cancelled = lambda: None
    times = iter([0, 91])
    monkeypatch.setattr(dns, "time", SimpleNamespace(monotonic=lambda: next(times)))
    with pytest.raises(StepFailedError, match="could not run"):
        dns.run_lookup(ctx, addon_store, p)
    assert not any("/jobs/" in path for path in addon_store.objects)


def test_phase6_input_validation_and_unknown_cluster(
    client: TestClient, addon_store: AddonStore, cluster_id: str
) -> None:
    assert client.post(f"{API}/clusters/{cluster_id}/dns/lookup", json={"hostname": "--help"}).status_code == 422
    assert client.post(f"{API}/clusters/{cluster_id}/dns/lookup", json={"hostname": "x;id"}).status_code == 422
    assert client.post(f"{API}/clusters/{cluster_id}/storage/nfs", json={"storage_class": "nfs"}).status_code == 422
    assert client.get(f"{API}/clusters/{uuid.uuid4()}/storage/nfs").status_code == 404
