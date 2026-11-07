"""Phase 4: kube-vip control-plane endpoints, the post-install smoke test, node add/remove,
clusters on provisioned machines, and node management (cordon, labels, taints, drain, logs)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.errors import ValidationFailedError
from app.modules.infrastructure.host_access import set_connector
from app.modules.kubernetes import nodes as knodes
from tests.fake_hosts import FakeCluster, FakeHost, host_key
from tests.fake_store import FakeStore
from tests.test_cluster_lifecycle import fleet, machines, spec, ssh_credential  # noqa: F401 - fixtures
from tests.test_provisioning_api import engine, local_connection, ssh_key_credential  # noqa: F401 - fixtures
from tests.test_workloads import cluster_id, store  # noqa: F401 - fixtures

API = "/api/v1"
VIP = "10.0.0.100"


def create(client: TestClient, body: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    response = client.post(f"{API}/clusters", json={"spec": body})
    assert response.status_code == 202, response.text
    created = response.json()
    cluster = client.get(f"{API}/clusters/{created['cluster']['id']}").json()
    return cluster, created["operation"]


def vip_spec(machines: dict[str, str], **overrides: Any) -> dict[str, Any]:
    return spec(
        machines,
        control_plane={
            "count": 1,
            "existing_machine_ids": [machines["cp-1"]],
            "load_balancer": "kube-vip",
            "endpoint": VIP,
        },
        worker_pools=[],
        **overrides,
    )


def host(fleet: FakeCluster, name: str) -> FakeHost:
    return next(h for h in fleet.hosts.values() if h.name == name)


def step_keys(op: dict[str, Any]) -> list[str]:
    return [s["key"] for s in op["steps"]]


# ---- kube-vip and smoke test ------------------------------------------------------------------


class TestKubeVip:
    def test_kube_vip_cluster(self, client: TestClient, machines: dict[str, str], fleet: FakeCluster) -> None:
        cluster, op = create(client, vip_spec(machines))
        assert op["status"] == "succeeded", op["error"]
        assert cluster["api_endpoint"] == f"https://{VIP}:6443"
        cp = host(fleet, "cp-1")
        scripts = [name for name, _ in cp.calls]
        # kube-vip holds the address before kubeadm init and is switched back to admin.conf after it.
        assert scripts.index("kube_vip") < scripts.index("init") < len(scripts) - 1 - scripts[::-1].index("kube_vip")
        manifest = json.loads(cp.kube_vip_manifest or "{}")
        env = {e["name"]: e.get("value") for e in manifest["spec"]["containers"][0]["env"]}
        assert env["address"] == VIP and env["vip_interface"] == "eth0" and env["cp_enable"] == "true"
        assert manifest["spec"]["volumes"][0]["hostPath"]["path"] == "/etc/kubernetes/admin.conf"
        assert manifest["spec"]["hostNetwork"] is True
        assert "smoke-test" in step_keys(op) and fleet.smoke_pods

    def test_bootstrap_uses_super_admin(self, client: TestClient, machines: dict[str, str], fleet: FakeCluster) -> None:
        create(client, vip_spec(machines))
        first = next(env for name, env in host(fleet, "cp-1").calls if name == "kube_vip")
        import base64

        manifest = json.loads(base64.b64decode(first["MANIFEST_B64"]))
        assert manifest["spec"]["volumes"][0]["hostPath"]["path"] == "/etc/kubernetes/super-admin.conf"
        # Before init nothing can answer on the VIP; after the switch to admin.conf the step waits
        # until the API answers through the VIP again (kube-vip restarts in between).
        calls = [env for name, env in host(fleet, "cp-1").calls if name == "kube_vip"]
        assert calls[0]["VIP"] == "" and calls[-1]["VIP"] == VIP

    def test_vip_must_be_on_the_node_subnet(self, client: TestClient, machines: dict[str, str]) -> None:
        body = vip_spec(machines)
        body["control_plane"]["endpoint"] = "192.168.50.10"
        _, op = create(client, body)
        assert op["status"] == "failed"
        assert "not in this machine's network" in op["error"]["message"]

    def test_vip_must_be_an_address(self, client: TestClient, machines: dict[str, str]) -> None:
        body = vip_spec(machines)
        body["control_plane"]["endpoint"] = "api.example.com"
        result = client.post(f"{API}/clusters/validate", json={"spec": body}).json()
        assert any("unused IPv4 address" in p for p in result["problems"])

    def test_smoke_test_failure_then_retry(
        self, client: TestClient, machines: dict[str, str], fleet: FakeCluster
    ) -> None:
        fleet.dns_works = False
        cluster, op = create(client, vip_spec(machines))
        assert op["status"] == "failed" and op["error"]["step"] == "smoke-test"
        assert "did not resolve to 10.96.0.1" in op["error"]["message"]
        assert client.get(f"{API}/clusters/{cluster['id']}").json()["lifecycle_state"] == "error"
        fleet.dns_works = True
        retried = client.post(f"{API}/operations/{op['id']}/retry").json()
        assert retried["status"] == "succeeded", retried["error"]
        assert client.get(f"{API}/clusters/{cluster['id']}").json()["lifecycle_state"] == "active"


# ---- add / remove nodes ------------------------------------------------------------------------


def worker_spec(machines: dict[str, str]) -> dict[str, Any]:
    return spec(
        machines,
        worker_pools=[
            {"name": "general", "count": 1, "existing_machine_ids": [machines["worker-1"]], "labels": {"tier": "apps"}}
        ],
    )


class TestAddRemoveNodes:
    def test_add_and_remove_worker(self, client: TestClient, machines: dict[str, str], fleet: FakeCluster) -> None:
        cluster, op = create(client, worker_spec(machines))
        assert op["status"] == "succeeded", op["error"]
        added = client.post(
            f"{API}/clusters/{cluster['id']}/nodes",
            json={"pool": "general", "existing_machine_ids": [machines["worker-2"]]},
        )
        assert added.status_code == 202, added.text
        add_op = added.json()
        assert add_op["status"] == "succeeded", add_op["error"]
        assert step_keys(add_op)[:1] == ["validate"] and "join-workers" in step_keys(add_op)
        assert "init" in step_keys(add_op) and "smoke-test" not in step_keys(add_op)
        assert host(fleet, "worker-2").joined
        detail = client.get(f"{API}/clusters/{cluster['id']}").json()
        assert detail["spec"]["worker_pools"][0]["count"] == 2
        assert sorted(n["node_name"] for n in detail["nodes"]) == ["cp-1", "worker-1", "worker-2"]

        assert client.delete(f"{API}/clusters/{cluster['id']}/nodes/worker-2").status_code == 428
        removed = client.delete(f"{API}/clusters/{cluster['id']}/nodes/worker-2?confirm=worker-2")
        assert removed.status_code == 202, removed.text
        assert removed.json()["status"] == "succeeded", removed.json()["error"]
        assert step_keys(removed.json()) == ["drain", "reset", "delete-nodes", "release"]
        assert "worker-2" in fleet.drained and "worker-2" in fleet.deleted_nodes
        assert not host(fleet, "worker-2").joined
        detail = client.get(f"{API}/clusters/{cluster['id']}").json()
        assert detail["spec"]["worker_pools"][0]["count"] == 1
        machine = client.get(f"{API}/machines/{machines['worker-2']}").json()
        assert machine["cluster_name"] is None

        audit = [e["action"] for e in client.get(f"{API}/audit-events").json()["items"]]
        assert "cluster.nodes.add" in audit and "cluster.nodes.remove" in audit

    def test_add_new_pool_with_labels(self, client: TestClient, machines: dict[str, str]) -> None:
        cluster, _ = create(client, worker_spec(machines))
        response = client.post(
            f"{API}/clusters/{cluster['id']}/nodes",
            json={
                "pool": "gpu",
                "existing_machine_ids": [machines["worker-2"]],
                "labels": {"accelerator": "gpu"},
                "taints": [{"key": "gpu", "effect": "NoSchedule"}],
            },
        )
        assert response.json()["status"] == "succeeded", response.json()["error"]
        pools = {p["name"]: p for p in client.get(f"{API}/clusters/{cluster['id']}").json()["spec"]["worker_pools"]}
        assert pools["gpu"]["labels"] == {"accelerator": "gpu"} and pools["gpu"]["count"] == 1

    def test_control_planes_need_an_endpoint(self, client: TestClient, machines: dict[str, str]) -> None:
        cluster, _ = create(client, worker_spec(machines))
        response = client.post(
            f"{API}/clusters/{cluster['id']}/nodes",
            json={"pool": "control-plane", "existing_machine_ids": [machines["worker-2"]]},
        )
        assert response.status_code == 422
        assert "no stable API endpoint" in response.json()["error"]["message"]

    def test_machines_must_be_free_and_ready(self, client: TestClient, machines: dict[str, str]) -> None:
        cluster, _ = create(client, worker_spec(machines))
        response = client.post(
            f"{API}/clusters/{cluster['id']}/nodes",
            json={"pool": "general", "existing_machine_ids": [machines["worker-1"]]},
        )
        assert response.status_code == 422
        assert any("already belongs" in p for p in response.json()["error"]["details"]["problems"])

    def test_add_and_remove_control_plane_with_kube_vip(
        self, client: TestClient, machines: dict[str, str], fleet: FakeCluster
    ) -> None:
        cluster, op = create(client, vip_spec(machines))
        assert op["status"] == "succeeded", op["error"]
        added = client.post(
            f"{API}/clusters/{cluster['id']}/nodes",
            json={"pool": "control-plane", "existing_machine_ids": [machines["worker-1"]]},
        ).json()
        assert added["status"] == "succeeded", added["error"]
        assert any(k.startswith("join-cp:") for k in step_keys(added))
        new_cp = host(fleet, "worker-1")
        assert new_cp.control_plane and new_cp.kube_vip_manifest and "admin.conf" in new_cp.kube_vip_manifest

        # The original first control plane can leave; the new one coordinates.
        removed = client.delete(f"{API}/clusters/{cluster['id']}/nodes/cp-1?confirm=cp-1").json()
        assert removed["status"] == "succeeded", removed["error"]
        assert "etcd" in step_keys(removed)
        assert host(fleet, "cp-1").kube_vip_manifest is None
        last = client.delete(f"{API}/clusters/{cluster['id']}/nodes/worker-1?confirm=worker-1")
        assert last.status_code == 409 and "only control plane" in last.json()["error"]["message"]

    def test_force_remove_unreachable_control_plane(
        self, client: TestClient, machines: dict[str, str], fleet: FakeCluster
    ) -> None:
        cluster, _ = create(client, vip_spec(machines))
        client.post(
            f"{API}/clusters/{cluster['id']}/nodes",
            json={"pool": "control-plane", "existing_machine_ids": [machines["worker-1"]]},
        )
        host(fleet, "worker-1").reachable = False
        stuck = client.delete(f"{API}/clusters/{cluster['id']}/nodes/worker-1?confirm=worker-1").json()
        assert stuck["status"] == "failed"
        forced = client.delete(f"{API}/clusters/{cluster['id']}/nodes/worker-1?confirm=worker-1&force=true")
        # The failed removal left the node in a failed state; the forced one completes it.
        assert forced.json()["status"] == "succeeded", forced.json()["error"]
        assert fleet.etcd_removed, "the unreachable control plane must be removed from etcd explicitly"

    def test_imported_clusters_cannot_add_nodes(self, client: TestClient, store: FakeStore, cluster_id: str) -> None:
        response = client.post(f"{API}/clusters/{cluster_id}/nodes", json={"pool": "general", "provision_count": 1})
        assert response.status_code == 422 and "imported" in response.json()["error"]["message"]


# ---- clusters on provisioned machines ---------------------------------------------------------


@pytest.fixture
def local_fleet() -> Iterator[FakeCluster]:
    cluster = FakeCluster()
    cluster.add("127.0.0.1:40022", FakeHost("local-cp-1", "172.28.1.10", host_key("lcp")))
    cluster.add("127.0.0.1:40024", FakeHost("local-general-1", "172.28.1.11", host_key("lw1")))
    cluster.add("127.0.0.1:40026", FakeHost("local-general-2", "172.28.1.12", host_key("lw2")))
    set_connector(cluster.connector())
    yield cluster
    set_connector(None)


def test_cluster_on_provisioned_machines_and_destroy(
    client: TestClient,
    engine: Any,
    local_fleet: FakeCluster,
    ssh_key_credential: str,
) -> None:
    connection = local_connection(client)
    template = {"cpu_cores": 2, "memory_mb": 2048, "disk_gb": 20}
    body = {
        "name": "local",
        "distribution": "kubeadm",
        "provider_type": "local",
        "provider_connection_id": connection["id"],
        "machine_credential_id": ssh_key_credential,
        "kubernetes_version": "1.34",
        "network": {"cni": "flannel"},
        "control_plane": {"count": 1, "machine_template": template},
        "worker_pools": [{"name": "general", "count": 1, "machine_template": template}],
    }
    validation = client.post(f"{API}/clusters/validate", json={"spec": body}).json()
    assert validation["valid"], validation["problems"]
    assert any("local-cp-1, local-general-1" in n for n in validation["notes"])
    assert validation["steps"][0]["key"] == "wait-machines"

    cluster, op = create(client, body)
    assert op["status"] == "succeeded", op["error"]
    assert step_keys(op)[0] == "wait-machines"
    assert sorted(engine.containers_by_name) == ["kplatform-local-cp-1", "kplatform-local-general-1"]
    assert cluster["spec"]["options"]["kubelet_resolv_conf"] == "/etc/kplatform-node/resolv.conf"
    assert cluster["api_server_override"] == "https://127.0.0.1:40023"

    # Add a provisioned worker to the existing pool.
    added = client.post(f"{API}/clusters/{cluster['id']}/nodes", json={"pool": "general", "provision_count": 1}).json()
    assert added["status"] == "succeeded", added["error"]
    assert step_keys(added)[0] == "wait-machines"
    assert "kplatform-local-general-2" in engine.containers_by_name

    # Remove it and destroy its machine.
    removed = client.delete(
        f"{API}/clusters/{cluster['id']}/nodes/local-general-2?confirm=local-general-2&destroy_machine=true"
    ).json()
    assert removed["status"] == "succeeded", removed["error"]
    assert "kplatform-local-general-2" not in engine.containers_by_name

    deleted = client.delete(f"{API}/clusters/{cluster['id']}?confirm=local&destroy_machines=true").json()
    assert deleted["status"] == "succeeded", deleted["error"]
    assert step_keys(deleted)[-1] == "destroy-machines"
    assert engine.containers_by_name == {}
    assert [m["name"] for m in client.get(f"{API}/machines").json()] == []


# ---- node management through the Kubernetes API ----------------------------------------------


def node(name: str, taints: list[dict[str, str]] | None = None) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Node",
        "metadata": {"name": name, "labels": {"kubernetes.io/hostname": name}},
        "spec": {"taints": taints or []},
        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
    }


def pod(name: str, node_name: str, *, owner: str | None = "ReplicaSet", emptydir: bool = False) -> dict[str, Any]:
    meta: dict[str, Any] = {"name": name, "namespace": "default"}
    if owner:
        meta["ownerReferences"] = [{"kind": owner, "name": "o", "uid": "u", "controller": True}]
    spec_: dict[str, Any] = {"nodeName": node_name, "containers": [{"name": "c", "image": "x"}]}
    if emptydir:
        spec_["volumes"] = [{"name": "scratch", "emptyDir": {}}]
    return {"apiVersion": "v1", "kind": "Pod", "metadata": meta, "spec": spec_, "status": {"phase": "Running"}}


class TestNodeManagement:
    def test_cordon_labels_taints(self, client: TestClient, store: FakeStore, cluster_id: str) -> None:
        store.put(node("n1", [{"key": "node.kubernetes.io/not-ready", "effect": "NoExecute"}]))
        base = f"{API}/clusters/{cluster_id}/nodes/n1"
        assert client.post(f"{base}/cordon").json()["unschedulable"] is True
        assert store.objects["/api/v1/nodes/n1"]["spec"]["unschedulable"] is True
        assert client.post(f"{base}/uncordon").json()["unschedulable"] is False

        labels = client.patch(f"{base}/labels", json={"set": {"tier": "db"}, "remove": []}).json()
        assert labels["tier"] == "db"
        labels = client.patch(f"{base}/labels", json={"set": {}, "remove": ["tier"]}).json()
        assert "tier" not in labels
        protected = client.patch(f"{base}/labels", json={"set": {"kubernetes.io/hostname": "x"}, "remove": []})
        assert protected.status_code == 422

        taints = client.put(
            f"{base}/taints", json={"taints": [{"key": "dedicated", "value": "db", "effect": "NoSchedule"}]}
        )
        assert taints.status_code == 200, taints.text
        keys = {t["key"] for t in store.objects["/api/v1/nodes/n1"]["spec"]["taints"]}
        assert keys == {"dedicated", "node.kubernetes.io/not-ready"}  # the system taint is kept
        bad = client.put(f"{base}/taints", json={"taints": [{"key": "x", "effect": "Sometimes"}]})
        assert bad.status_code == 422

        audit = [e["action"] for e in client.get(f"{API}/audit-events").json()["items"]]
        assert {"node.cordon", "node.uncordon", "node.labels", "node.taints"} <= set(audit)

    def test_drain_operation(self, client: TestClient, store: FakeStore, cluster_id: str) -> None:
        store.put(node("n1"))
        store.put(pod("web-1", "n1"))
        store.put(pod("agent-1", "n1", owner="DaemonSet"))
        store.put(pod("other-node", "n2"))
        op = client.post(f"{API}/clusters/{cluster_id}/nodes/n1/drain", json={}).json()
        assert op["status"] == "succeeded", op["error"]
        assert "/api/v1/namespaces/default/pods/web-1" not in store.objects
        assert "/api/v1/namespaces/default/pods/agent-1" in store.objects  # DaemonSet pods stay
        assert "/api/v1/namespaces/default/pods/other-node" in store.objects
        assert store.objects["/api/v1/nodes/n1"]["spec"]["unschedulable"] is True

    def test_drain_refuses_unmanaged_and_emptydir_pods(
        self, client: TestClient, store: FakeStore, cluster_id: str
    ) -> None:
        store.put(node("n1"))
        store.put(pod("bare", "n1", owner=None))
        store.put(pod("cache", "n1", emptydir=True))
        op = client.post(f"{API}/clusters/{cluster_id}/nodes/n1/drain", json={}).json()
        assert op["status"] == "failed"
        assert "not managed by a controller" in op["error"]["message"] and "emptyDir" in op["error"]["message"]
        assert not store.objects["/api/v1/nodes/n1"]["spec"].get("unschedulable")  # nothing changed
        op = client.post(
            f"{API}/clusters/{cluster_id}/nodes/n1/drain", json={"force": True, "delete_emptydir_data": True}
        ).json()
        assert op["status"] == "succeeded", op["error"]

    def test_drain_waits_for_disruption_budgets(self, store: FakeStore) -> None:
        store.put(node("n1"))
        store.put(pod("db-0", "n1"))
        store.pdb_blocked.add("default/db-0")
        attempts = {"n": 0}

        def sleep(_: float) -> None:
            attempts["n"] += 1
            if attempts["n"] == 2:  # the budget allows a disruption after a while
                store.pdb_blocked.clear()

        logs: list[str] = []
        result = knodes.drain(store, "n1", knodes.DrainOptions(), log=logs.append, sleep=sleep)
        assert result["evicted"] == 1 and attempts["n"] >= 2

        store.put(pod("db-1", "n1"))
        store.pdb_blocked.add("default/db-1")
        clock = iter(range(0, 10_000, 100))
        with pytest.raises(ValidationFailedError, match="blocked by PodDisruptionBudgets: default/db-1"):
            knodes.drain(
                store,
                "n1",
                knodes.DrainOptions(timeout_seconds=300),
                log=logs.append,
                sleep=lambda _: None,
                clock=lambda: next(clock),
            )

    def test_node_logs(self, client: TestClient, machines: dict[str, str], fleet: FakeCluster) -> None:
        cluster, _ = create(client, worker_spec(machines))
        response = client.get(f"{API}/clusters/{cluster['id']}/nodes/cp-1/logs", params={"unit": "containerd"})
        assert response.status_code == 200 and "cp-1 containerd: started" in response.text
        bad_unit = client.get(f"{API}/clusters/{cluster['id']}/nodes/cp-1/logs", params={"unit": "sshd"})
        assert bad_unit.status_code == 422
        unknown = client.get(f"{API}/clusters/{cluster['id']}/nodes/nope/logs")
        assert unknown.status_code == 404

    def test_imported_clusters_have_no_node_logs(self, client: TestClient, store: FakeStore, cluster_id: str) -> None:
        response = client.get(f"{API}/clusters/{cluster_id}/nodes/n1/logs")
        assert response.status_code == 422
