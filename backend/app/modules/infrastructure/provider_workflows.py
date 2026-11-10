"""Workflows that drive infrastructure providers and network discovery.

provider.validate     check a provider connection and record its status
provider.discover     list the machines a provider knows about (read-only)
machine.provision     create a machine through its provider, wait for SSH, inspect it
machine.power         start / stop / reboot a provider-managed machine
machine.deprovision   destroy a platform-provisioned machine and drop it from the inventory
network.discover      probe address ranges for SSH servers

Provider calls block and are idempotent (see providers/base.py), so every step can
be retried or resumed after a worker crash.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.modules.credentials.models import Credential, CredentialKind
from app.modules.credentials.service import CredentialService
from app.modules.infrastructure import discovery
from app.modules.infrastructure.models import (
    ConnectionStatus,
    Machine,
    MachineLifecycleState,
    PowerState,
    ProviderConnection,
)
from app.modules.infrastructure.ssh import load_private_key
from app.modules.infrastructure.workflows import step_connect, step_inspect
from app.modules.providers.base import InfrastructureProvider
from app.modules.providers.http import ProviderApiError, ProviderUnreachableError
from app.modules.providers.registry import provider_registry
from app.modules.providers.types import (
    DiscoveredMachine,
    MachineRef,
    MachineSpecRequest,
    NetworkConfigRequest,
    PowerAction,
)
from app.orchestration.context import StepContext
from app.orchestration.models import Operation, OperationStatus
from app.orchestration.workflow import (
    OperationTarget,
    RetryableStepError,
    StepDefinition,
    StepFailedError,
    WorkflowDefinition,
    workflow_registry,
)

PROVIDER_VALIDATE = "provider.validate"
PROVIDER_DISCOVER = "provider.discover"
MACHINE_PROVISION = "machine.provision"
MACHINE_POWER = "machine.power"
MACHINE_NETWORK = "machine.network"
MACHINE_DEPROVISION = "machine.deprovision"
NETWORK_DISCOVER = "network.discover"
PENDING_PREFIX = "pending:"


# ---- shared helpers -----------------------------------------------------------------


def open_provider(session: Session, ctx: StepContext, connection: ProviderConnection) -> InfrastructureProvider:
    credential: bytes | None = None
    if connection.credential_id:
        credential = CredentialService(session, ctx.secret_store).reveal(
            connection.organization_id, connection.credential_id
        )
        ctx.redact(credential.decode(errors="ignore").strip())
    return provider_registry.create(connection.provider_type, connection.config, credential)


@contextmanager
def provider_for(ctx: StepContext, connection_id: uuid.UUID) -> Iterator[InfrastructureProvider]:
    with ctx.db() as session:
        connection = session.get(ProviderConnection, connection_id)
        if connection is None or connection.organization_id != ctx.organization_id:
            raise StepFailedError("The provider connection was deleted", code="connection_not_found")
        provider = open_provider(session, ctx, connection)
    try:
        yield provider
    except ProviderUnreachableError as exc:
        raise RetryableStepError(exc.message) from exc
    except ProviderApiError as exc:
        raise StepFailedError(exc.message, code=exc.code) from exc
    finally:
        provider.close()


def _machine(session: Session, machine_id: uuid.UUID) -> Machine:
    machine = session.get(Machine, machine_id)
    if machine is None:
        raise StepFailedError("The machine was removed from the inventory", code="machine_not_found")
    return machine


def _power(value: str) -> PowerState:
    return {"running": PowerState.RUNNING, "stopped": PowerState.STOPPED, "suspended": PowerState.SUSPENDED}.get(
        value, PowerState.UNKNOWN
    )


def _apply_discovered(machine: Machine, found: DiscoveredMachine) -> None:
    machine.external_id = found.external_id
    machine.power_state = _power(found.power_state)
    if found.addresses:
        machine.addresses = [a.model_dump() for a in found.addresses]
    metadata = dict(machine.provider_metadata or {})
    metadata.update(found.provider_metadata)
    if found.kube_api_endpoint:
        metadata["kube_api_host"] = found.kube_api_endpoint.host
        metadata["kube_api_port"] = str(found.kube_api_endpoint.port)
    machine.provider_metadata = metadata
    if found.ssh_endpoint:
        machine.ssh_host, machine.ssh_port = found.ssh_endpoint.host, found.ssh_endpoint.port
    elif not machine.ssh_host and found.addresses:
        internal = [a for a in found.addresses if a.type == "internal"] or found.addresses
        machine.ssh_host, machine.ssh_port = internal[0].address, 22


def _public_key(session: Session, ctx: StepContext, credential_id: uuid.UUID) -> str:
    credential = session.get(Credential, credential_id)
    if credential is None or credential.kind is not CredentialKind.SSH_PRIVATE_KEY:
        raise StepFailedError("Provisioning needs an SSH private key credential", code="wrong_credential_kind")
    pem = CredentialService(session, ctx.secret_store).reveal(ctx.organization_id, credential_id).decode()
    try:
        key = load_private_key(pem)
    except Exception as exc:
        raise StepFailedError(f"The SSH key credential cannot be loaded: {exc}", code="invalid_key") from exc
    return f"{key.get_name()} {key.get_base64()} kubernetes-platform"


# ---- provider.validate ----------------------------------------------------------------


class ConnectionParams(BaseModel):
    connection_id: uuid.UUID
    connection_name: str


def step_validate(ctx: StepContext) -> dict[str, Any]:
    p: ConnectionParams = ctx.params  # type: ignore[assignment]
    error: str | None = None
    try:
        with provider_for(ctx, p.connection_id) as provider:
            result = provider.validate_connection()
    except (StepFailedError, RetryableStepError) as exc:
        error = str(exc)
        result = None
    with ctx.db() as session:
        connection = session.get(ProviderConnection, p.connection_id)
        if connection is not None:
            connection.last_validated_at = datetime.now(UTC)
            connection.status = ConnectionStatus.CONNECTED if result and result.ok else ConnectionStatus.ERROR
            connection.status_message = result.message if result else error
    if result is None:
        raise StepFailedError(error or "Validation failed", code="validation_failed")
    for key, value in result.details.items():
        ctx.info(f"{key}: {value}")
    ctx.set_result({"ok": result.ok, "message": result.message, "details": result.details})
    if not result.ok:
        raise StepFailedError(result.message, code="validation_failed", details=result.details)
    ctx.info(result.message)
    return {"ok": True}


workflow_registry.register(
    WorkflowDefinition(
        type=PROVIDER_VALIDATE,
        title="Validate provider connection",
        description="Authenticate against the provider and check that it is usable.",
        params_model=ConnectionParams,
        plan=lambda _p: [StepDefinition("validate", "Connect and validate", step_validate)],
        target=lambda p: OperationTarget(type="provider_connection", id=str(p.connection_id), name=p.connection_name),
        title_for=lambda p: f"Validate provider connection {p.connection_name}",
    )
)


# ---- provider.discover ----------------------------------------------------------------


def step_discover(ctx: StepContext) -> dict[str, Any]:
    p: ConnectionParams = ctx.params  # type: ignore[assignment]
    with provider_for(ctx, p.connection_id) as provider:
        found = provider.discover()
    with ctx.db() as session:
        known = {
            external_id: name
            for external_id, name in session.execute(
                select(Machine.external_id, Machine.name).where(Machine.provider_connection_id == p.connection_id)
            ).all()
        }
    machines = []
    for item in found:
        entry = item.model_dump(mode="json")
        entry["inventory_machine"] = known.get(item.external_id)
        machines.append(entry)
    ctx.info(f"Found {len(machines)} machine(s); {sum(1 for m in machines if m['inventory_machine'])} in the inventory")
    ctx.set_result({"machines": machines, "discovered_at": datetime.now(UTC).isoformat()})
    return {"count": len(machines)}


workflow_registry.register(
    WorkflowDefinition(
        type=PROVIDER_DISCOVER,
        title="Discover machines",
        description="List the machines the provider knows about (read-only).",
        params_model=ConnectionParams,
        plan=lambda _p: [StepDefinition("discover", "List machines", step_discover, max_attempts=2)],
        target=lambda p: OperationTarget(
            type="provider_connection", id=str(p.connection_id), name=p.connection_name, exclusive=False
        ),
        title_for=lambda p: f"Discover machines on {p.connection_name}",
    )
)


# ---- machine.provision ----------------------------------------------------------------


class ProvisionParams(BaseModel):
    machine_id: uuid.UUID
    machine_name: str
    connection_id: uuid.UUID
    credential_id: uuid.UUID
    cpu_cores: int = Field(ge=1, le=512)
    memory_mb: int = Field(ge=512)
    disk_gb: int = Field(ge=10)
    ssh_username: str = "platform"
    network: str | None = None
    image: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    provider_options: dict[str, Any] = Field(default_factory=dict)
    # Consumed by the shared connect/inspect steps.
    accept_new_host_key: bool = False


def step_provision(ctx: StepContext) -> dict[str, Any]:
    p: ProvisionParams = ctx.params  # type: ignore[assignment]
    with ctx.db() as session:
        machine = _machine(session, p.machine_id)
        machine.lifecycle_state = MachineLifecycleState.PROVISIONING
        public_key = _public_key(session, ctx, p.credential_id)
    request = MachineSpecRequest(
        name=p.machine_name,
        cpu_cores=p.cpu_cores,
        memory_mb=p.memory_mb,
        disk_gb=p.disk_gb,
        network=p.network,
        image=p.image,
        labels=p.labels,
        provider_options=p.provider_options,
        ssh_username=p.ssh_username,
        ssh_public_key=public_key,
    )
    ctx.info(f"Requesting {p.cpu_cores} CPU, {p.memory_mb} MB memory, {p.disk_gb} GB disk")
    with provider_for(ctx, p.connection_id) as provider:
        found = provider.provision(request)
    with ctx.db() as session:
        clash = session.scalar(
            select(Machine.name).where(
                Machine.provider_connection_id == p.connection_id,
                Machine.external_id == found.external_id,
                Machine.id != p.machine_id,
            )
        )
        if clash:
            raise StepFailedError(f"The provider returned {found.external_id}, already registered as '{clash}'")
        machine = _machine(session, p.machine_id)
        _apply_discovered(machine, found)
    ctx.info(f"Machine {found.external_id} created ({found.power_state})")
    return {"external_id": found.external_id}


def step_wait_address(ctx: StepContext) -> dict[str, Any]:
    p: ProvisionParams = ctx.params  # type: ignore[assignment]
    with ctx.db() as session:
        machine = _machine(session, p.machine_id)
        if machine.ssh_host:
            return {"host": machine.ssh_host, "port": machine.ssh_port}
        external_id = machine.external_id
    with provider_for(ctx, p.connection_id) as provider:
        status = provider.get_status(MachineRef(external_id=external_id))
    if not status.exists:
        raise StepFailedError("The machine disappeared from the provider", code="machine_gone")
    if not status.addresses and not status.ssh_endpoint:
        raise RetryableStepError("No IP address reported yet (waiting for the guest agent / DHCP)")
    with ctx.db() as session:
        machine = _machine(session, p.machine_id)
        _apply_discovered(
            machine,
            DiscoveredMachine(
                external_id=external_id,
                name=machine.name,
                addresses=status.addresses,
                power_state=status.power_state,
                ssh_endpoint=status.ssh_endpoint,
                kube_api_endpoint=status.kube_api_endpoint,
            ),
        )
        host, port = machine.ssh_host, machine.ssh_port
    ctx.info(f"Address {host}")
    return {"host": host, "port": port}


def step_wait_ssh(ctx: StepContext) -> dict[str, Any]:
    p: ProvisionParams = ctx.params  # type: ignore[assignment]
    with ctx.db() as session:
        machine = _machine(session, p.machine_id)
        host, port = machine.ssh_host, machine.ssh_port
    if not host:
        raise StepFailedError("The machine has no address", code="no_address")
    result = discovery.probe(host, port, timeout=5)
    if result is None:
        raise RetryableStepError(f"SSH is not answering on {host}:{port} yet")
    ctx.info(f"SSH answering on {host}:{port}: {result.banner}")
    return {"banner": result.banner}


def on_provision_status(session: Session, op: Operation) -> None:
    machine = session.get(Machine, uuid.UUID(op.params["machine_id"]))
    if machine is None:
        return
    if op.status is OperationStatus.RUNNING and machine.lifecycle_state is MachineLifecycleState.REQUESTED:
        machine.lifecycle_state = MachineLifecycleState.PROVISIONING
    elif op.status in (OperationStatus.FAILED, OperationStatus.CANCELLED):
        if machine.lifecycle_state in (MachineLifecycleState.REQUESTED, MachineLifecycleState.PROVISIONING):
            machine.lifecycle_state = MachineLifecycleState.ERROR
        machine.last_error = (op.error or {}).get("message") or "Provisioning was cancelled"


workflow_registry.register(
    WorkflowDefinition(
        type=MACHINE_PROVISION,
        title="Provision machine",
        description="Create a machine through its provider, wait for SSH and inspect it.",
        params_model=ProvisionParams,
        plan=lambda _p: [
            StepDefinition("provision", "Create the machine", step_provision, max_attempts=3, retry_backoff_seconds=10),
            StepDefinition(
                "wait-address", "Wait for an IP address", step_wait_address, max_attempts=60, retry_backoff_seconds=5
            ),
            StepDefinition("wait-ssh", "Wait for SSH", step_wait_ssh, max_attempts=60, retry_backoff_seconds=3),
            StepDefinition(
                "connect", "Connect over SSH and record host key", step_connect, max_attempts=5, retry_backoff_seconds=5
            ),
            StepDefinition(
                "inspect",
                "Inspect OS, hardware and prerequisites",
                step_inspect,
                max_attempts=2,
                retry_backoff_seconds=5,
            ),
        ],
        target=lambda p: OperationTarget(type="machine", id=str(p.machine_id), name=p.machine_name),
        title_for=lambda p: f"Provision machine {p.machine_name}",
        on_status=on_provision_status,
    )
)


# ---- machine.power ----------------------------------------------------------------------


class PowerParams(BaseModel):
    machine_id: uuid.UUID
    machine_name: str
    connection_id: uuid.UUID
    action: PowerAction


def step_power(ctx: StepContext) -> dict[str, Any]:
    p: PowerParams = ctx.params  # type: ignore[assignment]
    with ctx.db() as session:
        external_id = _machine(session, p.machine_id).external_id
    with provider_for(ctx, p.connection_id) as provider:
        status = provider.set_power_state(MachineRef(external_id=external_id), p.action)
    with ctx.db() as session:
        machine = _machine(session, p.machine_id)
        machine.power_state = _power(status.power_state)
        machine.last_seen_at = datetime.now(UTC)
    ctx.info(
        f"{p.action.value.capitalize()} request accepted for {p.machine_name}; "
        f"observed power state: {status.power_state}"
    )
    ctx.set_result({"power_state": status.power_state})
    return {"power_state": status.power_state}


class NetworkParams(BaseModel):
    machine_id: uuid.UUID
    machine_name: str
    connection_id: uuid.UUID
    request: NetworkConfigRequest


def step_network(ctx: StepContext) -> dict[str, Any]:
    p: NetworkParams = ctx.params  # type: ignore[assignment]
    with ctx.db() as session:
        external_id = _machine(session, p.machine_id).external_id
    with provider_for(ctx, p.connection_id) as provider:
        provider.configure_network(MachineRef(external_id=external_id), p.request)
    ctx.info("Provider accepted the network change. Inspect the machine after the provider finishes applying it.")
    return {"network": p.request.network}


workflow_registry.register(
    WorkflowDefinition(
        type=MACHINE_NETWORK,
        title="Configure machine network",
        params_model=NetworkParams,
        description="Apply a provider-supported network change to a platform-managed machine.",
        plan=lambda p: [StepDefinition("network", "Configure network at the provider", step_network)],
        target=lambda p: OperationTarget(type="machine", id=str(p.machine_id), name=p.machine_name),
    )
)


workflow_registry.register(
    WorkflowDefinition(
        type=MACHINE_POWER,
        title="Change machine power state",
        description="Start, stop or reboot a provider-managed machine.",
        params_model=PowerParams,
        plan=lambda p: [StepDefinition("power", f"{p.action.value.capitalize()} the machine", step_power)],
        target=lambda p: OperationTarget(type="machine", id=str(p.machine_id), name=p.machine_name),
        title_for=lambda p: f"{p.action.value.capitalize()} machine {p.machine_name}",
    )
)


# ---- machine.deprovision --------------------------------------------------------------


class DeprovisionParams(BaseModel):
    machine_id: uuid.UUID
    machine_name: str
    connection_id: uuid.UUID


def step_destroy(ctx: StepContext) -> dict[str, Any]:
    p: DeprovisionParams = ctx.params  # type: ignore[assignment]
    with ctx.db() as session:
        machine = _machine(session, p.machine_id)
        machine.lifecycle_state = MachineLifecycleState.DEPROVISIONING
        external_id = machine.external_id
    with provider_for(ctx, p.connection_id) as provider:
        if external_id.startswith(PENDING_PREFIX):
            # Provisioning never reported an ID: look the machine up by name.
            match = next((m for m in provider.discover() if m.name == p.machine_name), None)
            if match is None:
                ctx.info("Nothing was created at the provider")
                return {"deleted": None}
            external_id = match.external_id
        provider.delete(MachineRef(external_id=external_id))
    ctx.info(f"Deleted {external_id} at the provider")
    return {"deleted": external_id}


def step_forget(ctx: StepContext) -> dict[str, Any]:
    p: DeprovisionParams = ctx.params  # type: ignore[assignment]
    with ctx.db() as session:
        machine = session.get(Machine, p.machine_id)
        if machine is not None:
            session.delete(machine)
    ctx.info(f"{p.machine_name} removed from the inventory")
    ctx.set_result({"machine": p.machine_name, "deleted": ctx.outputs.get("destroy", {}).get("deleted")})
    return {}


def on_deprovision_status(session: Session, op: Operation) -> None:
    if op.status is not OperationStatus.FAILED:
        return
    machine = session.get(Machine, uuid.UUID(op.params["machine_id"]))
    if machine is not None:
        machine.lifecycle_state = MachineLifecycleState.ERROR
        machine.last_error = (op.error or {}).get("message")


workflow_registry.register(
    WorkflowDefinition(
        type=MACHINE_DEPROVISION,
        title="Destroy machine",
        description="Delete a platform-provisioned machine at its provider and remove it from the inventory.",
        params_model=DeprovisionParams,
        plan=lambda _p: [
            StepDefinition("destroy", "Delete at the provider", step_destroy, max_attempts=3, retry_backoff_seconds=10),
            StepDefinition("forget", "Remove from the inventory", step_forget),
        ],
        target=lambda p: OperationTarget(type="machine", id=str(p.machine_id), name=p.machine_name),
        title_for=lambda p: f"Destroy machine {p.machine_name}",
        destructive=True,
        on_status=on_deprovision_status,
    )
)


# ---- network.discover -------------------------------------------------------------------


class NetworkDiscoverParams(BaseModel):
    ranges: list[str] = Field(min_length=1, max_length=32)
    ports: list[int] = Field(default_factory=lambda: [22], min_length=1, max_length=8)
    timeout_seconds: float = Field(default=1.5, ge=0.2, le=10)


def step_scan(ctx: StepContext) -> dict[str, Any]:
    p: NetworkDiscoverParams = ctx.params  # type: ignore[assignment]
    addresses = discovery.parse_targets(p.ranges, allow_public=get_settings().discovery_allow_public_ranges)
    ctx.info(f"Probing {len(addresses)} address(es) on port(s) {', '.join(map(str, p.ports))}")

    def progress(done: int, total: int) -> None:
        ctx.set_progress(done / total)
        ctx.info(f"{done}/{total} probes done")

    hosts = discovery.scan(
        addresses, p.ports, timeout=p.timeout_seconds, on_progress=progress, should_stop=ctx.is_cancel_requested
    )
    ctx.check_cancelled()
    with ctx.db() as session:
        registered = {
            (host, port): name
            for host, port, name in session.execute(
                select(Machine.ssh_host, Machine.ssh_port, Machine.name).where(
                    Machine.organization_id == ctx.organization_id
                )
            ).all()
        }
    for host in hosts:
        host["registered_as"] = registered.get((str(host["address"]), int(str(host["port"]))))
        ctx.info(
            f"{host['address']}:{host['port']}  {host['banner']}"
            + (f"  ({host['hostname']})" if host["hostname"] else "")
        )
    ctx.set_result({"hosts": hosts, "scanned": len(addresses) * len(p.ports), "ranges": p.ranges, "ports": p.ports})
    ctx.info(f"Found {len(hosts)} SSH server(s)")
    return {"found": len(hosts)}


def _scan_title(p: NetworkDiscoverParams) -> str:
    shown = ", ".join(p.ranges[:3]) + (f" (+{len(p.ranges) - 3})" if len(p.ranges) > 3 else "")
    return f"Network discovery {shown}"


workflow_registry.register(
    WorkflowDefinition(
        type=NETWORK_DISCOVER,
        title="Network discovery",
        description="Probe address ranges for SSH servers.",
        params_model=NetworkDiscoverParams,
        plan=lambda _p: [StepDefinition("scan", "Probe addresses", step_scan)],
        target=lambda _p: OperationTarget(type="network", id="discovery", name="Network discovery", exclusive=False),
        title_for=_scan_title,
    )
)
