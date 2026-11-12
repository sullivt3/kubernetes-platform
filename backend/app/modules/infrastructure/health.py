"""Read-only, authenticated SSH liveness probes; no inventory or lifecycle actions."""

from __future__ import annotations

import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.core.secrets import SecretStore
from app.modules.infrastructure.access import load_target
from app.modules.infrastructure.host_access import (
    CommandOptions,
    HostAccessError,
    HostKeyMismatchError,
    HostUnreachableError,
    open_host,
)
from app.modules.infrastructure.models import Machine, MachineConnectivity, MachineLifecycleState

logger = logging.getLogger(__name__)
INACTIVE = {MachineLifecycleState.DELETED, MachineLifecycleState.DEPROVISIONING, MachineLifecycleState.PROVISIONING}


def observed_connectivity(machine: Machine) -> MachineConnectivity:
    """A stopped scheduler must not leave a permanently green badge."""
    cutoff = datetime.now(UTC) - timedelta(seconds=3 * get_settings().machine_probe_interval_seconds)
    if machine.connectivity_checked_at is None or machine.connectivity_checked_at < cutoff:
        return MachineConnectivity.UNKNOWN
    return machine.connectivity_status


def _endpoint_version(machine: Machine) -> tuple:
    return (
        machine.ssh_host,
        machine.ssh_port,
        machine.ssh_username,
        machine.access_credential_id,
        machine.host_key_type,
        machine.host_key_data,
    )


def probe_machine(session_factory: sessionmaker[Session], store: SecretStore, machine_id: uuid.UUID) -> str:
    # A transaction-scoped, non-blocking advisory lock prevents overlapping beat
    # runs from probing the same machine. No machine row is locked during network I/O.
    with session_factory() as lock_session:
        key = int.from_bytes(machine_id.bytes[:8], "big", signed=True)
        if not lock_session.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": key}):
            return "skipped"
        with session_factory() as session:
            machine = session.get(Machine, machine_id)
            if machine is None or machine.lifecycle_state in INACTIVE:
                return "skipped"
            version = _endpoint_version(machine)
            started = datetime.now(UTC)
            status, error = MachineConnectivity.UNKNOWN, None
            try:
                if not machine.host_key_type or not machine.host_key_data:
                    error = "Inspect this machine to establish a trusted SSH connection"
                    target = None
                elif not machine.ssh_host or machine.access_credential_id is None:
                    error = "SSH access is not configured"
                    target = None
                else:
                    target = load_target(session, store, machine.organization_id, machine.id)
            except Exception:
                # Do not expose decrypted credentials or transport exception text.
                target = None
                error = "Could not load SSH access; check the saved credential"

        if target is not None:
            try:
                with open_host(target.endpoint, target.auth, connect_timeout=5) as executor:
                    result = executor.run("true", CommandOptions(sudo=False, timeout_seconds=5))
                status = MachineConnectivity.REACHABLE if result.ok else MachineConnectivity.UNREACHABLE
                error = None if result.ok else "SSH connectivity check failed"
            except HostKeyMismatchError:
                status, error = MachineConnectivity.UNREACHABLE, "SSH host key changed; inspect and verify the server"
            except HostUnreachableError:
                status, error = MachineConnectivity.UNREACHABLE, "Cannot reach the machine over SSH"
            except HostAccessError:
                status, error = MachineConnectivity.UNREACHABLE, "SSH access failed; check the saved credential"
            except Exception:
                error = "Connectivity check failed; inspect the machine"
                logger.warning("Unexpected machine probe failure for %s", machine_id)

        with session_factory() as session:
            machine = session.scalar(select(Machine).where(Machine.id == machine_id).with_for_update())
            if (
                machine is None
                or machine.lifecycle_state in INACTIVE
                or _endpoint_version(machine) != version
                or (machine.connectivity_checked_at is not None and machine.connectivity_checked_at > started)
            ):
                return "skipped"
            machine.connectivity_status = status
            machine.connectivity_checked_at = started
            machine.connectivity_error = error
            if status is MachineConnectivity.REACHABLE:
                machine.last_seen_at = datetime.now(UTC)
            session.commit()
        return status.value


def probe_machines(session_factory: sessionmaker[Session], store: SecretStore) -> dict[str, int]:
    with session_factory() as session:
        ids = list(session.scalars(select(Machine.id).where(Machine.lifecycle_state.not_in(INACTIVE))))
    counts: dict[str, int] = {}

    def probe(machine_id: uuid.UUID) -> str:
        try:
            return probe_machine(session_factory, store, machine_id)
        except Exception:
            logger.warning("Machine probe could not complete for %s", machine_id)
            return "failed"

    if ids:
        with ThreadPoolExecutor(max_workers=min(4, len(ids))) as pool:
            for status in pool.map(probe, ids):
                counts[status] = counts.get(status, 0) + 1
    return counts
