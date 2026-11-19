"""Collector reconciliation preserves ownership and checks rollout before removing old grants."""

import json
import uuid
from unittest.mock import Mock

import pytest

from app.core.errors import ConflictError
from app.modules.kubernetes import log_collection as lc
from tests.fake_store import FakeStore


def test_collection_reconcile_and_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    store = FakeStore()
    cluster = uuid.uuid4()
    owner = {lc.OWNER: str(cluster)}
    ns = "monitoring"
    deploy_path = f"/apis/apps/v1/namespaces/{ns}/deployments/alloy"
    store.objects[deploy_path] = {
        "metadata": {"name": "alloy", "annotations": owner, "resourceVersion": "1", "generation": 1},
        "spec": {"template": {"spec": {"serviceAccountName": "alloy"}}},
        "status": {"observedGeneration": 1, "updatedReplicas": 1, "availableReplicas": 1, "replicas": 1},
    }
    old = f"/apis/rbac.authorization.k8s.io/v1/namespaces/old/roles/{lc.ROLE}"
    store.objects[old] = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "Role",
        "metadata": {"name": lc.ROLE, "namespace": "old", "annotations": owner, "uid": "old-role"},
    }
    unrelated = old.replace("/old/", "/other/")
    store.objects[unrelated] = {
        **store.objects[old],
        "metadata": {"name": lc.ROLE, "namespace": "other", "annotations": {lc.OWNER: "other"}, "uid": "other"},
    }
    request = store.request

    def transport(method, path, **kwargs):
        body = kwargs.get("body")
        if method == "PUT":
            assert body["metadata"]["resourceVersion"] == "1"
            store.objects[path] = body
            return 200, json.dumps(body).encode()
        if path == deploy_path and method == "PATCH":
            return 200, json.dumps(store.objects[path]).encode()
        result = request(method, path, **kwargs)
        if method == "POST" and result[0] < 300:
            data = json.loads(result[1])
            data["metadata"]["resourceVersion"] = "1"
            store.objects[store.path_for(data)] = data
            return result[0], json.dumps(data).encode()
        return result

    monkeypatch.setattr(store, "request", transport)
    monkeypatch.setattr(lc, "workflow_api", lambda *args: store)
    ctx = Mock(
        params=lc.CollectionParams(
            cluster_id=cluster, cluster_name="test", collector_namespace=ns, namespaces=["default"]
        )
    )
    assert lc.reconcile(ctx) == {"namespaces": ["default"]}
    assert old not in store.objects
    assert unrelated in store.objects
    assert lc.reconcile(ctx) == {"namespaces": ["default"]}
    grants = [obj for path, obj in store.objects.items() if path.endswith("/roles/" + lc.ROLE) and "/default/" in path]
    assert len(grants) == 1
    assert grants[0]["rules"][1] == {"apiGroups": [""], "resources": ["pods/log"], "verbs": ["get"]}


def test_owned_resource_collision_is_rejected() -> None:
    store = FakeStore()
    store.objects["/test/role"] = {"metadata": {"annotations": {lc.OWNER: "other"}}}
    with pytest.raises(ConflictError):
        lc.put_owned(store, "/test/role", {"metadata": {}}, "mine")
