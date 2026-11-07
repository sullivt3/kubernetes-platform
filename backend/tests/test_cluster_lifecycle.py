"""Machine onboarding and kubeadm cluster lifecycle, end to end through the API with fake servers."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.modules.cluster_lifecycle.distributions.kubeadm import config as cfg
from app.modules.cluster_lifecycle.distributions.spec import ClusterSpec
from app.modules.infrastructure.host_access import set_connector
from tests.fake_hosts import FakeCluster, FakeHost, host_key

API = "/api/v1"
PRIVATE_KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n-----END OPENSSH PRIVATE KEY-----\n"


@pytest.fixture
def fleet() -> Iterator[FakeCluster]:
    cluster = FakeCluster()
    cluster.add("10.0.0.11:22", FakeHost("cp-1", "10.0.0.11", host_key("cp1")))
    cluster.add("10.0.0.12:22", FakeHost("worker-1", "10.0.0.12", host_key("w1")))
    cluster.add("10.0.0.13:22", FakeHost("worker-2", "10.0.0.13", host_key("w2")))
    set_connector(cluster.connector())
    yield cluster
    set_connector(None)


def ssh_credential(client: TestClient) -> str:
    response = client.post(
        f"{API}/credentials",
        json={
            "name": "fleet-key",
            "kind": "ssh_private_key",
            "secret_value": PRIVATE_KEY,
            "public_metadata": {"username": "ops"},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def register(client: TestClient, credential_id: str, name: str, host: str) -> dict[str, Any]:
    response = client.post(
        f"{API}/machines", json={"name": name, "host": host, "credential_id": credential_id}
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
def machines(client: TestClient, fleet: FakeCluster) -> dict[str, str]:
    credential_id = ssh_credential(client)
    ids = {}
    for name, host in (("cp-1", "10.0.0.11"), ("worker-1", "10.0.0.12"), ("worker-2", "10.0.0.13")):
        ids[name] = register(client, credential_id, name, host)["machine"]["id"]
    return ids


def spec(machines: dict[str, str], **overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "name": "prod",
        "distribution": "kubeadm",
        "provider_type": "ssh",
        "kubernetes_version": "1.34",
        "network": {"cni": "calico", "pod_cidr": "10.244.0.0/16"},
        "control_plane": {"count": 1, "existing_machine_ids": [machines["cp-1"]]},
        "worker_pools": [
            {
                "name": "general",
                "count": 2,
                "existing_machine_ids": [machines["worker-1"], machines["worker-2"]],
                "labels": {"tier": "apps"},
                "taints": [{"key": "dedicated", "value": "apps", "effect": "NoSchedule"}],
            }
        ],
    }
    data.update(overrides)
    return data


def all_logs(client: TestClient, operation_id: str) -> str:
    return "\n".join(e["message"] for e in client.get(f"{API}/operations/{operation_id}/logs").json())


# ----------------------------------------------------------------------------- machines


class TestMachines:
    def test_registration_inspects_and_pins_host_key(
        self, client: TestClient, machines: dict[str, str]
    ) -> None:
        machine = client.get(f"{API}/machines/{machines['cp-1']}").json()
        assert machine["lifecycle_state"] == "ready"
        assert machine["host_key_fingerprint"] == "SHA256:cp1"
        assert machine["ssh_username"] == "ops"  # from credential metadata
        assert machine["hardware"]["cpu_cores"] == 4
        assert {"type": "primary", "address": "10.0.0.11"} in machine["addresses"]
        assert all(c["status"] == "pass" for c in machine["checks"])

    def test_changed_host_key_is_rejected_until_explicitly_accepted(
        self, client: TestClient, machines: dict[str, str], fleet: FakeCluster
    ) -> None:
        fleet.hosts["10.0.0.11:22"].host_key = host_key("rebuilt")
        op = client.post(f"{API}/machines/{machines['cp-1']}/inspect", json={}).json()
        assert op["status"] == "failed" and op["error"]["code"] == "host_key_mismatch"
        assert client.get(f"{API}/machines/{machines['cp-1']}").json()["lifecycle_state"] == "error"

        url = f"{API}/machines/{machines['cp-1']}/inspect"
        assert client.post(url, json={"accept_new_host_key": True}).status_code == 428
        op = client.post(url, params={"confirm": "cp-1"}, json={"accept_new_host_key": True}).json()
        assert op["status"] == "succeeded"
        machine = client.get(f"{API}/machines/{machines['cp-1']}").json()
        assert machine["host_key_fingerprint"] == "SHA256:rebuilt" and machine["lifecycle_state"] == "ready"
        assert "Accepted NEW SSH host key" in all_logs(client, op["id"])

    def test_unreachable_machine(self, client: TestClient, fleet: FakeCluster) -> None:
        fleet.hosts["10.0.0.11:22"].reachable = False
        credential_id = ssh_credential(client)
        result = register(client, credential_id, "cp-1", "10.0.0.11")
        assert result["operation"]["status"] == "failed"
        assert (
            client.get(f"{API}/machines/{result['machine']['id']}").json()["lifecycle_state"] == "unreachable"
        )

    def test_failed_prerequisites_block_readiness(self, client: TestClient, fleet: FakeCluster) -> None:
        fleet.hosts["10.0.0.11:22"].os_id = "gentoo"
        fleet.hosts["10.0.0.11:22"].sudo_ok = False
        result = register(client, ssh_credential(client), "cp-1", "10.0.0.11")
        machine = client.get(f"{API}/machines/{result['machine']['id']}").json()
        assert machine["lifecycle_state"] == "error"
        failed = {c["key"] for c in machine["checks"] if c["status"] == "fail"}
        assert failed == {"os", "sudo"}

    def test_registration_validation(self, client: TestClient, fleet: FakeCluster) -> None:
        credential_id = ssh_credential(client)
        bad = client.post(
            f"{API}/machines", json={"name": "Bad_Name", "host": "10.0.0.11", "credential_id": credential_id}
        )
        assert bad.status_code == 422
        register(client, credential_id, "cp-1", "10.0.0.11")
        duplicate = client.post(
            f"{API}/machines", json={"name": "other", "host": "10.0.0.11", "credential_id": credential_id}
        )
        assert duplicate.status_code == 409
        kubeconfig = client.post(
            f"{API}/credentials",
            json={"name": "kc", "kind": "kubeconfig", "secret_value": "clusters: []\ncontexts: []\n"},
        ).json()
        wrong_kind = client.post(
            f"{API}/machines", json={"name": "x", "host": "10.0.0.12", "credential_id": kubeconfig["id"]}
        )
        assert wrong_kind.status_code == 422


# ----------------------------------------------------------------------------- validation


class TestValidation:
    def test_valid_spec_previews_plan(self, client: TestClient, machines: dict[str, str]) -> None:
        result = client.post(f"{API}/clusters/validate", json={"spec": spec(machines)}).json()
        assert result["valid"], result["problems"]
        keys = [s["key"] for s in result["steps"]]
        assert keys[:3] == ["validate", "prepare", "containerd"]
        assert "join-workers" in keys and keys[-1] == "register"
        assert [n["role"] for n in result["nodes"]] == ["control_plane", "worker", "worker"]

    @pytest.mark.parametrize(
        ("change", "fragment"),
        [
            ({"network": {"cni": "cilium"}}, "CNI 'cilium' is not supported"),
            ({"kubernetes_version": "1.27"}, "not supported; use 1.30"),
            ({"provider_type": "proxmox"}, "cannot run on 'proxmox'"),
        ],
    )
    def test_unsupported_choices(
        self, client: TestClient, machines: dict[str, str], change: dict[str, Any], fragment: str
    ) -> None:
        result = client.post(f"{API}/clusters/validate", json={"spec": spec(machines, **change)}).json()
        assert not result["valid"]
        assert any(fragment in p for p in result["problems"]), result["problems"]

    def test_machine_requirements(
        self, client: TestClient, machines: dict[str, str], fleet: FakeCluster
    ) -> None:
        fleet.hosts["10.0.0.11:22"].cpus = 1
        client.post(f"{API}/machines/{machines['cp-1']}/inspect", json={})
        result = client.post(f"{API}/clusters/validate", json={"spec": spec(machines)}).json()
        assert any("needs at least 2 CPUs" in p for p in result["problems"])

    def test_provisioning_not_available(self, client: TestClient, machines: dict[str, str]) -> None:
        pools = [
            {
                "name": "auto",
                "count": 2,
                "machine_template": {"cpu_cores": 2, "memory_mb": 4096, "disk_gb": 40},
            }
        ]
        result = client.post(
            f"{API}/clusters/validate", json={"spec": spec(machines, worker_pools=pools)}
        ).json()
        assert any("cannot provision new machines" in p for p in result["problems"])

    def test_create_rejects_invalid_spec(self, client: TestClient, machines: dict[str, str]) -> None:
        response = client.post(f"{API}/clusters", json={"spec": spec(machines, network={"cni": "cilium"})})
        assert response.status_code == 422
        assert response.json()["error"]["details"]["problems"]


# ----------------------------------------------------------------------------- create / delete


class TestClusterLifecycle:
    def test_create_cluster_end_to_end(
        self, client: TestClient, machines: dict[str, str], fleet: FakeCluster
    ) -> None:
        response = client.post(f"{API}/clusters", json={"spec": spec(machines)})
        assert response.status_code == 202, response.text
        op = response.json()["operation"]
        cluster_id = response.json()["cluster"]["id"]
        assert op["status"] == "succeeded", op["error"]
        assert all(s["status"] == "succeeded" for s in op["steps"])

        cluster = client.get(f"{API}/clusters/{cluster_id}").json()
        assert cluster["lifecycle_state"] == "active"
        assert cluster["kubernetes_version"] == "v1.34.7"
        assert cluster["api_endpoint"] == "https://10.0.0.11:6443"
        assert cluster["has_kubeconfig"] is True
        assert {n["machine_name"]: n["membership_state"] for n in cluster["nodes"]} == {
            "cp-1": "joined",
            "worker-1": "joined",
            "worker-2": "joined",
        }

        cp, w1 = fleet.hosts["10.0.0.11:22"], fleet.hosts["10.0.0.12:22"]
        assert [c[0] for c in cp.calls] == ["prepare", "containerd", "packages", "packages", "init", "cni"]
        assert [c[0] for c in w1.calls] == ["prepare", "containerd", "packages", "join"]
        assert all(h.installed_version == "1.34.7" for h in fleet.hosts.values())
        assert dict(w1.calls[0][1])["NODE_ROLE"] == "worker"

        # Kubeconfig stored encrypted; never exposed through the API.
        credentials = client.get(f"{API}/credentials").json()["items"]
        kubeconfig = next(c for c in credentials if c["name"] == "cluster-prod-admin")
        assert kubeconfig["kind"] == "kubeconfig"
        detail = client.get(f"{API}/clusters/{cluster_id}")
        assert "SECRETKEYDATA" not in detail.text

        # Bootstrap tokens are redacted from every log line.
        logs = all_logs(client, op["id"])
        secrets = cp.secrets_seen + w1.secrets_seen
        assert secrets, "fake hosts should have echoed tokens"
        assert not any(secret in logs for secret in secrets)
        assert "--token ********" in logs
        assert "SECRETKEYDATA" not in logs

        # Machines now show their cluster assignment and cannot be removed.
        listed = {m["name"]: m for m in client.get(f"{API}/machines").json()}
        assert listed["cp-1"]["cluster_name"] == "prod" and listed["cp-1"]["cluster_role"] == "control_plane"
        blocked = client.delete(f"{API}/machines/{machines['cp-1']}", params={"confirm": "cp-1"})
        assert blocked.status_code == 409

    def test_machine_cannot_join_two_clusters(self, client: TestClient, machines: dict[str, str]) -> None:
        client.post(f"{API}/clusters", json={"spec": spec(machines)})
        result = client.post(f"{API}/clusters/validate", json={"spec": spec(machines, name="second")}).json()
        assert any("already belongs to cluster 'prod'" in p for p in result["problems"])

    def test_failure_marks_cluster_error_and_retry_resumes(
        self, client: TestClient, machines: dict[str, str], fleet: FakeCluster
    ) -> None:
        fleet.hosts["10.0.0.13:22"].fail_next["join"] = 1  # script failures fail the step immediately
        response = client.post(f"{API}/clusters", json={"spec": spec(machines)}).json()
        op, cluster_id = response["operation"], response["cluster"]["id"]
        assert op["status"] == "failed"
        assert op["error"]["step"] == "join-workers"
        assert "worker-2" in op["error"]["message"]
        cluster = client.get(f"{API}/clusters/{cluster_id}").json()
        assert cluster["lifecycle_state"] == "error"
        assert cluster["status_detail"]["error"]["step"] == "join-workers"

        retried = client.post(f"{API}/operations/{op['id']}/retry").json()
        assert retried["status"] == "succeeded", retried["error"]
        assert client.get(f"{API}/clusters/{cluster_id}").json()["lifecycle_state"] == "active"
        cp_scripts = [c[0] for c in fleet.hosts["10.0.0.11:22"].calls]
        assert cp_scripts.count("init") == 1, "completed steps must not re-run on retry"

    def test_delete_requires_confirmation_and_releases_machines(
        self, client: TestClient, machines: dict[str, str], fleet: FakeCluster
    ) -> None:
        cluster_id = client.post(f"{API}/clusters", json={"spec": spec(machines)}).json()["cluster"]["id"]
        url = f"{API}/clusters/{cluster_id}"
        assert client.delete(url).status_code == 428
        op = client.delete(url, params={"confirm": "prod"}).json()
        assert op["status"] == "succeeded", op["error"]
        assert client.get(url).status_code == 404
        assert all(h.calls[-1][0] == "reset" for h in fleet.hosts.values())
        assert all(m["cluster_name"] is None for m in client.get(f"{API}/machines").json())
        names = [c["name"] for c in client.get(f"{API}/credentials").json()["items"]]
        assert "cluster-prod-admin" not in names
        # Machines are free for a new cluster.
        assert client.post(f"{API}/clusters/validate", json={"spec": spec(machines)}).json()["valid"]

    def test_delete_with_unreachable_node(
        self, client: TestClient, machines: dict[str, str], fleet: FakeCluster
    ) -> None:
        cluster_id = client.post(f"{API}/clusters", json={"spec": spec(machines)}).json()["cluster"]["id"]
        fleet.hosts["10.0.0.13:22"].reachable = False
        url = f"{API}/clusters/{cluster_id}"
        failed = client.delete(url, params={"confirm": "prod"}).json()
        assert failed["status"] == "failed"
        assert client.get(url).json()["lifecycle_state"] == "error"
        forced = client.delete(url, params={"confirm": "prod", "force": True}).json()
        assert forced["status"] == "succeeded"
        assert client.get(url).status_code == 404


# ----------------------------------------------------------------------------- config rendering


def _docs(text: str) -> list[dict[str, Any]]:
    return [json.loads(part) for part in text.split("\n---\n")]


def _spec(**overrides: Any) -> ClusterSpec:
    base: dict[str, Any] = {
        "name": "c1",
        "distribution": "kubeadm",
        "provider_type": "ssh",
        "kubernetes_version": "1.34",
        "control_plane": {"count": 1, "existing_machine_ids": ["00000000-0000-0000-0000-000000000001"]},
    }
    base.update(overrides)
    return ClusterSpec.model_validate(base)


def test_init_config_v1beta4() -> None:
    docs = _docs(
        cfg.render_init_config(
            spec=_spec(),
            options=cfg.KubeadmOptions(allow_swap=True, ignore_preflight_errors=["Swap"]),
            version="1.34.2",
            node_name="cp-1",
            advertise_address="10.0.0.11",
            bootstrap_token="abcdef.0123456789abcdef",
            certificate_key=None,
        )
    )
    init, cluster, kubelet = docs
    assert init["apiVersion"] == "kubeadm.k8s.io/v1beta4"
    assert init["nodeRegistration"]["kubeletExtraArgs"] == [{"name": "node-ip", "value": "10.0.0.11"}]
    assert init["nodeRegistration"]["ignorePreflightErrors"] == ["Swap"]
    assert cluster["kubernetesVersion"] == "v1.34.2"
    assert cluster["networking"] == {
        "podSubnet": "10.244.0.0/16",
        "serviceSubnet": "10.96.0.0/12",
        "dnsDomain": "cluster.local",
    }
    assert "controlPlaneEndpoint" not in cluster
    assert kubelet["cgroupDriver"] == "systemd" and kubelet["failSwapOn"] is False


def test_init_config_v1beta3_and_ha_endpoint() -> None:
    ha = _spec(
        kubernetes_version="1.30",
        control_plane={
            "count": 3,
            "load_balancer": "external",
            "endpoint": "api.lab.internal",
            "existing_machine_ids": [f"00000000-0000-0000-0000-00000000000{i}" for i in (1, 2, 3)],
        },
    )
    init, cluster, kubelet = _docs(
        cfg.render_init_config(
            spec=ha,
            options=cfg.KubeadmOptions(),
            version="1.30.9",
            node_name="cp-1",
            advertise_address="10.0.0.11",
            bootstrap_token="abcdef.0123456789abcdef",
            certificate_key="ff" * 32,
        )
    )
    assert init["apiVersion"] == "kubeadm.k8s.io/v1beta3"
    assert init["nodeRegistration"]["kubeletExtraArgs"] == {"node-ip": "10.0.0.11"}
    assert init["certificateKey"] == "ff" * 32
    assert cluster["controlPlaneEndpoint"] == "api.lab.internal:6443"
    assert cluster["apiServer"]["certSANs"] == ["api.lab.internal"]
    assert "failSwapOn" not in kubelet


def test_join_config_for_control_plane() -> None:
    (join,) = _docs(
        cfg.render_join_config(
            version="1.34.2",
            options=cfg.KubeadmOptions(),
            node_name="cp-2",
            advertise_address="10.0.0.12",
            api_server_endpoint="api:6443",
            token="abcdef.0123456789abcdef",
            ca_cert_hash="sha256:aa",
            certificate_key="ee" * 32,
        )
    )
    assert join["discovery"]["bootstrapToken"] == {
        "apiServerEndpoint": "api:6443",
        "token": "abcdef.0123456789abcdef",
        "caCertHashes": ["sha256:aa"],
    }
    assert join["controlPlane"]["certificateKey"] == "ee" * 32


def test_generated_secrets_have_kubeadm_format() -> None:
    import re

    assert re.fullmatch(r"[a-z0-9]{6}\.[a-z0-9]{16}", cfg.generate_bootstrap_token())
    assert re.fullmatch(r"[0-9a-f]{64}", cfg.generate_certificate_key())


def test_options_validation() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        cfg.KubeadmOptions(ignore_preflight_errors=["Swap; rm -rf /"])
    with pytest.raises(ValidationError):
        cfg.KubeadmOptions(api_server_extra_sans=["bad name with spaces"])


def test_kube_proxy_conntrack_option() -> None:
    docs = _docs(
        cfg.render_init_config(
            spec=_spec(),
            options=cfg.KubeadmOptions(kube_proxy_conntrack_max_per_core=0),
            version="1.37.2",
            node_name="cp-1",
            advertise_address="10.0.0.11",
            bootstrap_token="abcdef.0123456789abcdef",
            certificate_key=None,
        )
    )
    assert docs[-1] == {
        "apiVersion": "kubeproxy.config.k8s.io/v1alpha1",
        "kind": "KubeProxyConfiguration",
        "conntrack": {"maxPerCore": 0},
    }
    default = _docs(
        cfg.render_init_config(
            spec=_spec(),
            options=cfg.KubeadmOptions(),
            version="1.37.2",
            node_name="cp-1",
            advertise_address="10.0.0.11",
            bootstrap_token="abcdef.0123456789abcdef",
            certificate_key=None,
        )
    )
    assert all(d["kind"] != "KubeProxyConfiguration" for d in default)


def test_kubelet_resolv_conf_option() -> None:
    docs = _docs(
        cfg.render_init_config(
            spec=_spec(),
            options=cfg.KubeadmOptions(kubelet_resolv_conf="/etc/kplatform/resolv.conf"),
            version="1.37.2",
            node_name="cp-1",
            advertise_address="10.0.0.11",
            bootstrap_token="abcdef.0123456789abcdef",
            certificate_key=None,
        )
    )
    assert docs[2]["resolvConf"] == "/etc/kplatform/resolv.conf"
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        cfg.KubeadmOptions(kubelet_resolv_conf="relative/path; rm")


def test_loopback_dns_is_flagged() -> None:
    from app.modules.infrastructure import facts

    base = {
        "os_id": "ubuntu",
        "os_version_id": "24.04",
        "arch": "x86_64",
        "systemd": "yes",
        "cpu_cores": "2",
        "memory_mb": "4096",
        "primary_ip": "10.0.0.5",
    }
    loop = {
        c.key: c
        for c in facts.evaluate(
            {**base, "nameservers": "127.0.0.11", "systemd_resolved": "inactive"}, sudo_ok=True
        )
    }
    assert loop["dns"].status is facts.CheckStatus.WARN
    resolved = {
        c.key: c
        for c in facts.evaluate(
            {**base, "nameservers": "127.0.0.53", "systemd_resolved": "active"}, sudo_ok=True
        )
    }
    assert resolved["dns"].status is facts.CheckStatus.PASS
