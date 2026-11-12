"""Catch unusable runtimes before joining and check CNI on every joined node."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.modules.cluster_lifecycle.distributions.kubeadm import steps
from tests.fake_hosts import FakeCluster, FakeExecutor
from tests.test_cluster_lifecycle import API, all_logs, spec
from tests.test_cluster_lifecycle import fleet as fleet
from tests.test_cluster_lifecycle import machines as machines


def test_broken_pause_image_fails_before_initialization_and_can_be_retried(
    client: TestClient, machines: dict[str, str], fleet: FakeCluster
) -> None:
    worker = fleet.hosts["10.0.0.12:22"]
    worker.runtime_ok = False
    result = client.post(f"{API}/clusters", json={"spec": spec(machines)}).json()
    op = result["operation"]
    assert op["status"] == "failed" and op["error"]["step"] == "packages"
    assert "worker-1" in op["error"]["message"] and "pause image" in op["error"]["message"]
    assert all(not h.initialized and not h.joined for h in fleet.hosts.values())
    worker.runtime_ok = True
    retry = client.post(f"{API}/operations/{op['id']}/retry").json()
    assert retry["status"] == "succeeded", retry["error"]


@pytest.mark.parametrize("cni", ["calico", "flannel"])
def test_passing_initial_rollout_cannot_hide_a_failed_worker_network(
    client: TestClient, machines: dict[str, str], fleet: FakeCluster, monkeypatch: pytest.MonkeyPatch, cni: str
) -> None:
    monkeypatch.setattr(steps, "CNI_FAILURE_GRACE_SECONDS", 0)
    fleet.network_failures["worker-2"] = "RunContainerError"
    result = client.post(f"{API}/clusters", json={"spec": spec(machines, network={"cni": cni})}).json()
    op = result["operation"]
    assert next(s for s in op["steps"] if s["key"] == "cni")["status"] == "succeeded"
    assert op["status"] == "failed" and op["error"]["step"] == "wait-ready"
    assert op["error"]["code"] == "cni_failed"
    assert "worker-2" in op["error"]["message"] and "RunContainerError" in op["error"]["message"]
    assert "2/3 " + cni + " pods Ready" in all_logs(client, op["id"])
    fleet.network_failures.clear()
    retry = client.post(f"{API}/operations/{op['id']}/retry").json()
    assert retry["status"] == "succeeded", retry["error"]


def test_initial_network_startup_failure_can_recover_within_grace_period(
    client: TestClient, machines: dict[str, str], fleet: FakeCluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    fleet.network_failures["worker-1"] = "CrashLoopBackOff"
    original = FakeExecutor._kubectl

    def recovering(executor: FakeExecutor, command: str):
        result = original(executor, command)
        if "get pods -l" in command:
            fleet.network_failures.clear()
        return result

    monkeypatch.setattr(FakeExecutor, "_kubectl", recovering)
    monkeypatch.setattr(steps.time, "sleep", lambda _seconds: None)
    result = client.post(f"{API}/clusters", json={"spec": spec(machines)}).json()
    assert result["operation"]["status"] == "succeeded", result["operation"]["error"]
    assert "CrashLoopBackOff" in all_logs(client, result["operation"]["id"])


def test_network_readiness_ignores_terminating_and_wrong_node_pods() -> None:
    pods = [
        {
            "metadata": {"name": "old", "deletionTimestamp": "now"},
            "spec": {"nodeName": "worker"},
            "status": {"conditions": [{"type": "Ready", "status": "True"}]},
        },
        {
            "metadata": {"name": "other"},
            "spec": {"nodeName": "not-selected"},
            "status": {"conditions": [{"type": "Ready", "status": "True"}]},
        },
    ]
    ready, problems, failing = steps._cni_readiness(pods, {"worker"})
    assert ready == set() and failing == {"worker"}
    assert "no networking pod" in problems["worker"]


def test_network_readiness_requires_healthy_init_containers() -> None:
    pod = {
        "metadata": {"name": "calico-worker"},
        "spec": {"nodeName": "worker"},
        "status": {"initContainerStatuses": [{"name": "upgrade-ipam", "state": {"terminated": {"exitCode": 255}}}]},
    }
    ready, problems, failing = steps._cni_readiness([pod], {"worker"})
    assert ready == set() and failing == {"worker"}
    assert "upgrade-ipam: exited 255" in problems["worker"]


def test_dns_smoke_check_reaches_each_node_and_detects_worker_only_failure(
    client: TestClient, machines: dict[str, str], fleet: FakeCluster
) -> None:
    fleet.dns_failures.add("worker-2")
    result = client.post(f"{API}/clusters", json={"spec": spec(machines)}).json()
    op = result["operation"]
    assert op["status"] == "failed" and op["error"]["step"] == "smoke-test"
    assert "worker-2" in op["error"]["message"]
    assert set(fleet.smoke_nodes.values()) == {"cp-1", "worker-1", "worker-2"}
    assert set(fleet.deleted_smoke_pods) == set(fleet.smoke_pods)
    assert next(s for s in op["steps"] if s["key"] == "wait-ready")["status"] == "succeeded"
    fleet.dns_failures.clear()
    retry = client.post(f"{API}/operations/{op['id']}/retry").json()
    assert retry["status"] == "succeeded", retry["error"]


def test_dns_server_address_is_not_mistaken_for_the_expected_service_answer(
    client: TestClient, machines: dict[str, str], fleet: FakeCluster
) -> None:
    fleet.smoke_logs = "Server: 10.96.0.10\nAddress: 10.96.0.10:53\n"
    result = client.post(f"{API}/clusters", json={"spec": spec(machines)}).json()
    op = result["operation"]
    assert op["status"] == "failed" and op["error"]["code"] == "smoke_test_failed"
    assert set(fleet.deleted_smoke_pods) == set(fleet.smoke_pods)
