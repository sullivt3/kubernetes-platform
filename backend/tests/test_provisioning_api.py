"""Provider connections, provisioning, power, destroy and network discovery through the API.

The local provider runs against a fake Docker engine and the provisioned machines'
SSH servers are fake hosts, so the full workflow (provision -> wait for SSH ->
pin host key -> inspect) executes for real.
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from fastapi.testclient import TestClient

from app.modules.infrastructure import provider_workflows
from app.modules.infrastructure.discovery import ProbeResult
from app.modules.infrastructure.host_access import set_connector
from app.modules.providers.implementations import local as local_mod
from tests.fake_docker import FakeDocker
from tests.fake_hosts import FakeCluster, FakeHost, host_key

API = "/api/v1"


@pytest.fixture
def ssh_key_credential(client: TestClient) -> str:
    pem = (
        ed25519.Ed25519PrivateKey.generate()
        .private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption())
        .decode()
    )
    response = client.post(
        f"{API}/credentials", json={"name": "lab-key", "kind": "ssh_private_key", "secret_value": pem}
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeDocker]:
    fake = FakeDocker()
    ports = iter(range(40022, 40100))
    monkeypatch.setattr(local_mod.LocalProvider, "_docker", lambda self: fake)
    monkeypatch.setattr(local_mod.LocalProvider, "_free_port", staticmethod(lambda: next(ports)))
    # SSH "answers" on every published port; the fake fleet decides what is behind it.
    monkeypatch.setattr(
        provider_workflows.discovery,
        "probe",
        lambda host, port, timeout: ProbeResult(host, port, "SSH-2.0-OpenSSH_9.6p1 Ubuntu-3ubuntu13.5", "Ubuntu", None),
    )
    yield fake


@pytest.fixture
def fleet() -> Iterator[FakeCluster]:
    cluster = FakeCluster()
    # Machines get SSH on 127.0.0.1:40022, :40024 ... (API ports in between).
    cluster.add("127.0.0.1:40022", FakeHost("dev-1", "172.28.1.10", host_key("dev1")))
    cluster.add("127.0.0.1:40024", FakeHost("dev-2", "172.28.1.11", host_key("dev2")))
    set_connector(cluster.connector())
    yield cluster
    set_connector(None)


def local_connection(client: TestClient) -> dict[str, Any]:
    response = client.post(f"{API}/provider-connections", json={"name": "Workstation", "provider_type": "local"})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["operation"]["status"] == "succeeded", body["operation"]
    return dict(body["connection"])


def test_connection_rules(client: TestClient, engine: FakeDocker, ssh_key_credential: str) -> None:
    planned = client.post(
        f"{API}/provider-connections",
        json={"name": "vc", "provider_type": "vsphere", "config": {"server": "vc", "datacenter": "dc"}},
    )
    assert planned.status_code == 422 and "configuration" in planned.json()["error"]["message"]
    ssh = client.post(f"{API}/provider-connections", json={"name": "ssh2", "provider_type": "ssh"})
    assert ssh.status_code == 422
    wrong_kind = client.post(
        f"{API}/provider-connections",
        json={
            "name": "pve",
            "provider_type": "proxmox",
            "credential_id": ssh_key_credential,
            "config": {
                "api_url": "https://pve:8006",
                "token_id": "root@pam!kp",
                "node": "pve1",
                "template_vmid": 9000,
            },
        },
    )
    assert wrong_kind.status_code == 422 and "proxmox_api_token" in wrong_kind.json()["error"]["message"]
    bad_config = client.post(
        f"{API}/provider-connections", json={"name": "x1", "provider_type": "local", "config": {"access": "teleport"}}
    )
    assert bad_config.status_code == 422

    connection = local_connection(client)
    assert connection["status"] == "connected"
    assert client.get(f"{API}/provider-connections/{connection['id']}").json()["status"] == "connected"
    duplicate = client.post(f"{API}/provider-connections", json={"name": "Workstation", "provider_type": "local"})
    assert duplicate.status_code == 409

    no_confirm = client.delete(f"{API}/provider-connections/{connection['id']}")
    assert no_confirm.status_code == 428
    assert client.delete(f"{API}/provider-connections/{connection['id']}?confirm=Workstation").status_code == 204


def test_provision_power_destroy(
    client: TestClient, engine: FakeDocker, fleet: FakeCluster, ssh_key_credential: str
) -> None:
    connection = local_connection(client)
    response = client.post(
        f"{API}/provider-connections/{connection['id']}/machines",
        json={
            "names": ["dev-1", "dev-2"],
            "cpu_cores": 2,
            "memory_mb": 2048,
            "disk_gb": 20,
            "credential_id": ssh_key_credential,
        },
    )
    assert response.status_code == 202, response.text
    results = response.json()
    assert [r["operation"]["status"] for r in results] == ["succeeded", "succeeded"], results[0]["operation"]
    assert [s["key"] for s in results[0]["operation"]["steps"]] == [
        "provision",
        "wait-address",
        "wait-ssh",
        "connect",
        "inspect",
    ]

    machines = {m["name"]: m for m in client.get(f"{API}/machines").json()}
    dev1 = machines["dev-1"]
    assert dev1["lifecycle_state"] == "ready"
    assert dev1["provisioned"] and dev1["provider_type"] == "local" and dev1["connection_name"] == "Workstation"
    assert (dev1["ssh_host"], dev1["ssh_port"]) == ("127.0.0.1", 40022)
    assert dev1["external_id"] == "kplatform-dev-1"
    assert dev1["provider_metadata"]["kube_api_port"] == "40023"
    assert dev1["host_key_fingerprint"]
    # The machine's public key was injected; the private key never left the platform.
    env = engine.runs[0]["environment"]
    assert env["AUTHORIZED_KEY"].startswith("ssh-ed25519 ") and "PRIVATE" not in env["AUTHORIZED_KEY"]

    # Names are unique across the inventory.
    again = client.post(
        f"{API}/provider-connections/{connection['id']}/machines",
        json={
            "names": ["dev-1"],
            "cpu_cores": 2,
            "memory_mb": 2048,
            "disk_gb": 20,
            "credential_id": ssh_key_credential,
        },
    )
    assert again.status_code == 409

    stop = client.post(f"{API}/machines/{dev1['id']}/power", json={"action": "stop"})
    assert stop.status_code == 202 and stop.json()["status"] == "succeeded"
    assert client.get(f"{API}/machines/{dev1['id']}").json()["power_state"] == "stopped"
    assert engine.containers_by_name["kplatform-dev-1"].status == "exited"

    discovered = client.post(f"{API}/provider-connections/{connection['id']}/discover").json()
    found = {m["name"]: m["inventory_machine"] for m in discovered["result"]["machines"]}
    assert found == {"dev-1": "dev-1", "dev-2": "dev-2"}

    assert client.post(f"{API}/machines/{dev1['id']}/destroy").status_code == 428
    destroyed = client.post(f"{API}/machines/{dev1['id']}/destroy?confirm=dev-1")
    assert destroyed.status_code == 202 and destroyed.json()["status"] == "succeeded", destroyed.json()
    assert "kplatform-dev-1" not in engine.containers_by_name
    assert client.get(f"{API}/machines/{dev1['id']}").status_code == 404

    audit = [e["action"] for e in client.get(f"{API}/audit-events").json()["items"]]
    for action in ("provider_connection.create", "machine.provision", "machine.power.stop", "machine.destroy"):
        assert action in audit


def test_registered_servers_cannot_be_destroyed(
    client: TestClient, fleet: FakeCluster, ssh_key_credential: str
) -> None:
    fleet.add("10.9.0.5:22", FakeHost("srv", "10.9.0.5", host_key("srv")))
    machine = client.post(
        f"{API}/machines", json={"name": "srv", "host": "10.9.0.5", "credential_id": ssh_key_credential}
    ).json()["machine"]
    response = client.post(f"{API}/machines/{machine['id']}/destroy?confirm=srv")
    assert response.status_code == 400 and response.json()["error"]["code"] == "capability_not_supported"
    power = client.post(f"{API}/machines/{machine['id']}/power", json={"action": "reboot"})
    assert power.status_code == 400


def test_import_discovered_machine(
    client: TestClient, engine: FakeDocker, fleet: FakeCluster, ssh_key_credential: str
) -> None:
    connection = local_connection(client)
    response = client.post(
        f"{API}/provider-connections/{connection['id']}/import",
        json={
            "external_id": "kplatform-dev-1",
            "name": "dev-1",
            "host": "127.0.0.1",
            "port": 40022,
            "credential_id": ssh_key_credential,
            "kube_api_host": "127.0.0.1",
            "kube_api_port": 40023,
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["operation"]["status"] == "succeeded"
    machine = client.get(f"{API}/machines/{response.json()['machine']['id']}").json()
    assert machine["lifecycle_state"] == "ready" and machine["provider_metadata"]["kube_api_port"] == "40023"


def test_cluster_on_container_machines_gets_container_defaults(
    client: TestClient, engine: FakeDocker, fleet: FakeCluster, ssh_key_credential: str
) -> None:
    connection = local_connection(client)
    results = client.post(
        f"{API}/provider-connections/{connection['id']}/machines",
        json={
            "names": ["dev-1"],
            "cpu_cores": 2,
            "memory_mb": 2048,
            "disk_gb": 20,
            "credential_id": ssh_key_credential,
        },
    ).json()
    machine_id = results[0]["machine"]["id"]
    spec = {
        "name": "local",
        "distribution": "kubeadm",
        "provider_type": "ssh",
        "kubernetes_version": "1.34",
        "network": {"cni": "flannel", "pod_cidr": "10.244.0.0/16"},
        "control_plane": {"count": 1, "existing_machine_ids": [machine_id]},
        "worker_pools": [],
        "options": {"control_plane_workloads": True},
    }
    validation = client.post(f"{API}/clusters/validate", json={"spec": spec}).json()
    assert validation["valid"], validation["problems"]
    assert validation["notes"] and "dev-1" in validation["notes"][0]

    created = client.post(f"{API}/clusters", json={"spec": spec})
    assert created.status_code == 202, created.text
    cluster = client.get(f"{API}/clusters/{created.json()['cluster']['id']}").json()
    assert cluster["spec"]["options"]["kubelet_resolv_conf"] == "/etc/kplatform-node/resolv.conf"
    assert cluster["spec"]["options"]["allow_swap"] is True
    assert created.json()["operation"]["status"] == "succeeded", created.json()["operation"].get("error")
    # The published API port became the connection override, verified against the node IP.
    assert cluster["api_server_override"] == "https://127.0.0.1:40023"
    assert cluster["tls_server_name"] == "172.28.1.10"


def _ssh_server() -> tuple[socket.socket, int]:
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()

    def serve() -> None:
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            with conn:
                conn.sendall(b"SSH-2.0-OpenSSH_9.2p1 Debian-2+deb12u3\r\n")

    threading.Thread(target=serve, daemon=True).start()
    return server, server.getsockname()[1]


def test_network_discovery(client: TestClient) -> None:
    public = client.post(f"{API}/network-discovery", json={"ranges": ["1.1.1.1"]})
    assert public.status_code == 422 and "public" in public.json()["error"]["message"]
    server, port = _ssh_server()
    try:
        response = client.post(f"{API}/network-discovery", json={"ranges": ["127.0.0.1"], "ports": [port]})
    finally:
        server.close()
    assert response.status_code == 202, response.text
    operation = response.json()
    assert operation["status"] == "succeeded", operation
    [host] = operation["result"]["hosts"]
    assert (host["address"], host["port"], host["os_hint"]) == ("127.0.0.1", port, "Debian")
