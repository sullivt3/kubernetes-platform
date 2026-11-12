"""Machine probes report authenticated reachability without changing lifecycle state."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.secrets import get_secret_store
from app.db.session import get_session_factory
from app.modules.infrastructure import health
from app.modules.infrastructure.host_access import HostAccessError, HostEndpoint, set_connector
from app.modules.infrastructure.models import Machine, MachineConnectivity, MachineLifecycleState
from tests.fake_hosts import FakeCluster, host_key
from tests.test_cluster_lifecycle import API
from tests.test_cluster_lifecycle import fleet as fleet
from tests.test_cluster_lifecycle import machines as machines


def test_deleted_vm_becomes_unavailable_then_recovers(
    client: TestClient, machines: dict[str, str], fleet: FakeCluster
) -> None:
    machine_id = machines["worker-2"]
    url = f"{API}/machines/{machine_id}"
    before = client.get(url).json()
    vm = fleet.hosts.pop("10.0.0.13:22")
    assert health.probe_machine(get_session_factory(), get_secret_store(), uuid.UUID(machine_id)) == "unreachable"
    after = client.get(url).json()
    assert after["connectivity_status"] == "unreachable"
    assert after["connectivity_checked_at"] is not None
    assert after["last_seen_at"] == before["last_seen_at"]
    assert after["lifecycle_state"] == before["lifecycle_state"] == "ready"
    assert after["id"] == before["id"]
    fleet.add("10.0.0.13:22", vm)
    assert health.probe_machine(get_session_factory(), get_secret_store(), uuid.UUID(machine_id)) == "reachable"
    recovered = client.get(url).json()
    assert recovered["connectivity_status"] == "reachable"
    assert recovered["connectivity_error"] is None
    assert recovered["last_seen_at"] > before["last_seen_at"]


def test_reused_ip_cannot_silently_trust_another_server(
    client: TestClient, machines: dict[str, str], fleet: FakeCluster
) -> None:
    fleet.hosts["10.0.0.13:22"].host_key = host_key("replacement")
    mid = uuid.UUID(machines["worker-2"])
    assert health.probe_machine(get_session_factory(), get_secret_store(), mid) == "unreachable"
    machine = client.get(f"{API}/machines/{mid}").json()
    assert machine["host_key_fingerprint"] == "SHA256:w2"
    assert "host key changed" in machine["connectivity_error"]


def test_ssh_auth_failure_does_not_leak_transport_details(
    client: TestClient, machines: dict[str, str], fleet: FakeCluster
) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise HostAccessError("secret-password-must-not-appear")

    set_connector(refuse)
    mid = uuid.UUID(machines["worker-1"])
    assert health.probe_machine(get_session_factory(), get_secret_store(), mid) == "unreachable"
    response = client.get(f"{API}/machines/{mid}")
    assert "secret-password" not in response.text
    assert "saved credential" in response.json()["connectivity_error"]


def test_stale_results_are_unknown(client: TestClient, machines: dict[str, str], session: Session) -> None:
    machine = session.get(Machine, uuid.UUID(machines["worker-1"]))
    assert machine is not None
    machine.connectivity_checked_at = datetime.now(UTC) - timedelta(minutes=10)
    machine.connectivity_status = MachineConnectivity.REACHABLE
    session.commit()
    assert client.get(f"{API}/machines/{machine.id}").json()["connectivity_status"] == "unknown"
    assert (
        next(m for m in client.get(f"{API}/machines").json() if m["id"] == str(machine.id))["connectivity_status"]
        == "unknown"
    )


def test_monitor_does_not_establish_first_contact_trust(
    client: TestClient, machines: dict[str, str], session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine = session.get(Machine, uuid.UUID(machines["worker-1"]))
    assert machine is not None
    machine.host_key_data = None
    machine.host_key_type = None
    session.commit()

    def unexpected(*args: object, **kwargs: object) -> None:
        pytest.fail("An unpinned host must not be contacted by the background monitor")

    monkeypatch.setattr(health, "open_host", unexpected)
    assert health.probe_machine(get_session_factory(), get_secret_store(), machine.id) == "unknown"
    session.refresh(machine)
    assert machine.host_key_data is None


@pytest.mark.parametrize("state", [MachineLifecycleState.PROVISIONING, MachineLifecycleState.DEPROVISIONING])
def test_monitor_skips_provider_lifecycle_operations(
    machines: dict[str, str], session: Session, state: MachineLifecycleState
) -> None:
    machine = session.get(Machine, uuid.UUID(machines["worker-1"]))
    assert machine is not None
    machine.lifecycle_state = state
    session.commit()
    assert health.probe_machine(get_session_factory(), get_secret_store(), machine.id) == "skipped"


def test_changed_endpoint_discards_inflight_result(
    machines: dict[str, str], fleet: FakeCluster, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    mid = uuid.UUID(machines["worker-1"])

    def connect(endpoint: HostEndpoint, auth: object, **kwargs: object):
        with get_session_factory()() as other:
            machine = other.get(Machine, mid)
            assert machine is not None
            machine.ssh_host = "10.0.0.99"
            machine.connectivity_checked_at = None
            machine.connectivity_status = MachineConnectivity.UNKNOWN
            other.commit()
        return fleet.connector()(endpoint, auth)  # type: ignore[arg-type]

    monkeypatch.setattr(health, "open_host", connect)
    assert health.probe_machine(get_session_factory(), get_secret_store(), mid) == "skipped"
    session.expire_all()
    machine = session.get(Machine, mid)
    assert machine is not None and machine.connectivity_checked_at is None
    assert machine.connectivity_status == MachineConnectivity.UNKNOWN


def test_overlapping_probe_is_skipped(machines: dict[str, str]) -> None:
    mid = uuid.UUID(machines["worker-1"])
    with get_session_factory()() as locked:
        locked.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": int.from_bytes(mid.bytes[:8], "big", signed=True)}
        )
        assert health.probe_machine(get_session_factory(), get_secret_store(), mid) == "skipped"


def test_one_offline_machine_does_not_stop_the_fleet(machines: dict[str, str], fleet: FakeCluster) -> None:
    del fleet.hosts["10.0.0.13:22"]
    assert health.probe_machines(get_session_factory(), get_secret_store()) == {"reachable": 2, "unreachable": 1}
