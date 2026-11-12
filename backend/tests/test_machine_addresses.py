"""Address changes preserve inventory identity and enforce the existing SSH trust."""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.auditing.models import AuditEvent
from app.modules.infrastructure.models import Machine
from app.modules.organizations.models import Organization
from app.orchestration.models import Operation, OperationStatus
from tests.fake_hosts import FakeCluster, FakeHost, host_key
from tests.test_cluster_lifecycle import API, spec
from tests.test_cluster_lifecycle import fleet as fleet
from tests.test_cluster_lifecycle import machines as machines


def test_move_preserves_identity_and_inspects_new_address(
    client: TestClient, machines: dict[str, str], fleet: FakeCluster, session: Session
) -> None:
    machine_id = machines["cp-1"]
    before = client.get(f"{API}/machines/{machine_id}").json()
    host = fleet.hosts.pop("10.0.0.11:22")
    host.ip = "10.0.0.21"
    fleet.add("10.0.0.21:22", host)
    response = client.patch(f"{API}/machines/{machine_id}/address", json={"host": " 10.0.0.21 "})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["operation"]["status"] == "succeeded", data
    after = data["machine"]
    for key in ("id", "name", "created_at", "access_credential_id", "ssh_port", "ssh_username", "host_key_fingerprint"):
        assert after[key] == before[key]
    assert after["ssh_host"] == "10.0.0.21"
    assert after["external_id"] == "10.0.0.21:22"
    assert after["addresses"][0]["address"] == "10.0.0.21"
    assert after["lifecycle_state"] == "ready"
    event = session.scalar(select(AuditEvent).where(AuditEvent.action == "machine.update_address"))
    assert event is not None
    assert event.details == {"previous_host": "10.0.0.11", "host": "10.0.0.21", "port": 22}


def test_move_rejects_different_host_key(client: TestClient, machines: dict[str, str], fleet: FakeCluster) -> None:
    fleet.add("10.0.0.21:22", FakeHost("other", "10.0.0.21", host_key("other-server")))
    response = client.patch(f"{API}/machines/{machines['cp-1']}/address", json={"host": "10.0.0.21"})
    assert response.status_code == 200
    data = response.json()
    assert data["operation"]["error"]["code"] == "host_key_mismatch"
    assert data["machine"]["host_key_fingerprint"] == "SHA256:cp1"
    assert data["machine"]["lifecycle_state"] == "error"
    assert data["machine"]["addresses"] == []
    assert data["machine"]["inspected_at"] is None


@pytest.mark.parametrize("host", ["", "bad host", "https://10.0.0.21", "10.0.0.21;id", "-bad", "x" * 254])
def test_invalid_address_does_not_change_machine(client: TestClient, machines: dict[str, str], host: str) -> None:
    url = f"{API}/machines/{machines['cp-1']}"
    assert client.patch(url + "/address", json={"host": host}).status_code == 422
    assert client.get(url).json()["ssh_host"] == "10.0.0.11"


def test_duplicate_address_does_not_change_machine(client: TestClient, machines: dict[str, str]) -> None:
    url = f"{API}/machines/{machines['cp-1']}"
    assert client.patch(url + "/address", json={"host": "10.0.0.12"}).status_code == 409
    assert client.get(url).json()["ssh_host"] == "10.0.0.11"


def test_cluster_membership_is_preserved(client: TestClient, machines: dict[str, str], fleet: FakeCluster) -> None:
    created = client.post(f"{API}/clusters", json={"spec": spec(machines)}).json()
    assert created["operation"]["status"] == "succeeded", created
    cluster_id = created["cluster"]["id"]
    before = client.get(f"{API}/clusters/{cluster_id}").json()
    host = fleet.hosts.pop("10.0.0.12:22")
    host.ip = "10.0.0.22"
    fleet.add("10.0.0.22:22", host)
    response = client.patch(f"{API}/machines/{machines['worker-1']}/address", json={"host": "10.0.0.22"})
    assert response.status_code == 200, response.text
    assert response.json()["machine"]["cluster_name"] == "prod"
    after = client.get(f"{API}/clusters/{cluster_id}").json()
    assert [(n["machine_id"], n["role"], n["membership_state"]) for n in after["nodes"]] == [
        (n["machine_id"], n["role"], n["membership_state"]) for n in before["nodes"]
    ]
    assert after["api_endpoint"] == before["api_endpoint"]


@pytest.mark.parametrize("target", ["machine", "cluster"])
def test_active_operation_blocks_address_change(
    client: TestClient, machines: dict[str, str], session: Session, target: str
) -> None:
    machine_id = machines["cp-1"]
    if target == "cluster":
        created = client.post(f"{API}/clusters", json={"spec": spec(machines)}).json()
        assert created["operation"]["status"] == "succeeded", created
        target_id = created["cluster"]["id"]
    else:
        target_id = machine_id
    operation = session.scalar(select(Operation).where(Operation.target_id == target_id))
    assert operation is not None
    operation.status = OperationStatus.PENDING
    session.commit()
    response = client.patch(f"{API}/machines/{machine_id}/address", json={"host": "10.0.0.21"})
    assert response.status_code == 409, response.text
    assert client.get(f"{API}/machines/{machine_id}").json()["ssh_host"] == "10.0.0.11"


def test_other_organization_machine_is_hidden(client: TestClient, machines: dict[str, str], session: Session) -> None:
    organization = Organization(name="Other", slug="other")
    session.add(organization)
    session.flush()
    machine = session.get(Machine, uuid.UUID(machines["cp-1"]))
    assert machine is not None
    machine.organization_id = organization.id
    session.commit()
    response = client.patch(f"{API}/machines/{machine.id}/address", json={"host": "10.0.0.21"})
    assert response.status_code == 404


def test_provisioned_machine_cannot_change_provider_identity(
    client: TestClient, machines: dict[str, str], session: Session
) -> None:
    machine = session.get(Machine, uuid.UUID(machines["cp-1"]))
    assert machine is not None
    machine.provisioned = True
    session.commit()
    response = client.patch(f"{API}/machines/{machine.id}/address", json={"host": "10.0.0.21"})
    assert response.status_code == 422
    session.expire_all()
    assert machine.external_id == "10.0.0.11:22"
