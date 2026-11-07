"""Machine inventory management."""

from __future__ import annotations

import re
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import ConfirmationRequiredError, ConflictError, NotFoundError, ValidationFailedError
from app.core.principal import Principal
from app.modules.auditing import service as audit
from app.modules.cluster_lifecycle.models import Cluster, ClusterMachine
from app.modules.credentials.models import Credential, CredentialKind
from app.modules.infrastructure.models import Machine, MachineLifecycleState, ProviderConnection
from app.modules.infrastructure.workflows import MACHINE_INSPECT
from app.modules.providers.registry import provider_registry
from app.modules.providers.types import ProviderType
from app.orchestration.models import Operation
from app.orchestration.service import OperationService

# Machine names become Kubernetes node names: RFC 1123 label.
NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.\-:]{0,251}[A-Za-z0-9])?$")
SSH_CONNECTION_NAME = "Linux servers (SSH)"


class MachineService:
    def __init__(self, session: Session, operations: OperationService) -> None:
        self.session = session
        self.operations = operations

    def ensure_ssh_connection(self, principal: Principal) -> ProviderConnection:
        connection = self.session.scalar(
            select(ProviderConnection).where(
                ProviderConnection.organization_id == principal.organization_id,
                ProviderConnection.provider_type == ProviderType.SSH,
                ProviderConnection.name == SSH_CONNECTION_NAME,
            )
        )
        if connection is None:
            connection = ProviderConnection(
                organization_id=principal.organization_id,
                name=SSH_CONNECTION_NAME,
                provider_type=ProviderType.SSH,
                config={},
            )
            self.session.add(connection)
            self.session.flush()
        return connection

    def register(
        self,
        principal: Principal,
        *,
        name: str,
        host: str,
        port: int | None,
        username: str | None,
        credential_id: uuid.UUID,
        labels: dict[str, str] | None = None,
    ) -> tuple[Machine, Operation]:
        if not NAME_RE.match(name):
            raise ValidationFailedError(
                "Machine names must be lowercase letters, digits and '-' (they become Kubernetes node names)"
            )
        if not HOST_RE.match(host):
            raise ValidationFailedError("Host must be a hostname or IP address")
        credential = self.session.get(Credential, credential_id)
        if credential is None or credential.organization_id != principal.organization_id:
            raise ValidationFailedError("Credential not found")
        if credential.kind not in (CredentialKind.SSH_PRIVATE_KEY, CredentialKind.SSH_PASSWORD):
            raise ValidationFailedError("Machines need an SSH private key or SSH password credential")

        connection = self.ensure_ssh_connection(principal)
        provider = provider_registry.create(ProviderType.SSH, connection.config, None)
        endpoint = provider.connection_endpoint(host, port, username or credential.public_metadata.get("username"))
        external_id = f"{endpoint.host}:{endpoint.port}"
        if self.session.scalar(
            select(func.count()).where(
                Machine.organization_id == principal.organization_id,
                (Machine.name == name) | (Machine.external_id == external_id),
            )
        ):
            raise ConflictError(f"A machine named '{name}' or at {external_id} is already registered")

        machine = Machine(
            organization_id=principal.organization_id,
            name=name,
            provider_connection_id=connection.id,
            external_id=external_id,
            lifecycle_state=MachineLifecycleState.REQUESTED,
            ssh_host=endpoint.host,
            ssh_port=endpoint.port,
            ssh_username=endpoint.username,
            access_credential_id=credential.id,
            labels=labels or {},
        )
        self.session.add(machine)
        self.session.flush()
        audit.record(
            self.session,
            principal=principal,
            action="machine.register",
            resource_type="machine",
            resource_id=machine.id,
            resource_name=name,
            details={"address": external_id, "username": endpoint.username, "credential": credential.name},
        )
        self.session.commit()
        operation = self.operations.submit(
            principal, MACHINE_INSPECT, {"machine_id": str(machine.id), "machine_name": name}
        )
        return self.get(principal, machine.id), operation

    def inspect(
        self, principal: Principal, machine_id: uuid.UUID, *, accept_new_host_key: bool, confirm: str | None
    ) -> Operation:
        machine = self.get(principal, machine_id)
        if accept_new_host_key and confirm != machine.name:
            raise ConfirmationRequiredError(
                "Accepting a new host key disables protection against impersonation for this connection. "
                "Repeat the machine name to confirm.",
                details={"expected_confirmation": machine.name},
            )
        return self.operations.submit(
            principal,
            MACHINE_INSPECT,
            {
                "machine_id": str(machine.id),
                "machine_name": machine.name,
                "accept_new_host_key": accept_new_host_key,
            },
        )

    def list_machines(self, principal: Principal) -> list[tuple[Machine, str | None, str | None]]:
        """Machines with (cluster name, role) when assigned."""
        rows = self.session.execute(
            select(Machine, Cluster.name, ClusterMachine.role)
            .outerjoin(ClusterMachine, ClusterMachine.machine_id == Machine.id)
            .outerjoin(Cluster, Cluster.id == ClusterMachine.cluster_id)
            .where(Machine.organization_id == principal.organization_id)
            .order_by(Machine.name)
        ).all()
        return [(m, c, r.value if r else None) for m, c, r in rows]

    def connections(self, principal: Principal) -> dict[uuid.UUID, tuple[str, str]]:
        """Connection id -> (name, provider type), for labelling machines."""
        rows = self.session.execute(
            select(ProviderConnection.id, ProviderConnection.name, ProviderConnection.provider_type).where(
                ProviderConnection.organization_id == principal.organization_id
            )
        ).all()
        return {cid: (name, ptype.value) for cid, name, ptype in rows}

    def get(self, principal: Principal, machine_id: uuid.UUID) -> Machine:
        machine = self.session.get(Machine, machine_id, populate_existing=True)
        if machine is None or machine.organization_id != principal.organization_id:
            raise NotFoundError("Machine not found")
        return machine

    def assignment(self, machine_id: uuid.UUID) -> tuple[str | None, str | None]:
        row = self.session.execute(
            select(Cluster.name, ClusterMachine.role)
            .join(ClusterMachine, ClusterMachine.cluster_id == Cluster.id)
            .where(ClusterMachine.machine_id == machine_id)
        ).first()
        return (row[0], row[1].value) if row else (None, None)

    def delete(self, principal: Principal, machine_id: uuid.UUID, *, confirm: str | None) -> None:
        machine = self.get(principal, machine_id)
        if confirm != machine.name:
            raise ConfirmationRequiredError(
                "Removing a machine from the inventory cannot be undone. Repeat its name to confirm.",
                details={"expected_confirmation": machine.name},
            )
        cluster, _ = self.assignment(machine.id)
        if cluster:
            raise ConflictError(f"Machine is a node of cluster '{cluster}'; delete the cluster first")
        active = self.operations.list_operations(
            principal, active_only=True, target_type="machine", target_id=str(machine.id), limit=1
        )[1]
        if active:
            raise ConflictError("An operation is in progress for this machine")
        audit.record(
            self.session,
            principal=principal,
            action="machine.delete",
            resource_type="machine",
            resource_id=machine.id,
            resource_name=machine.name,
            details={"address": machine.external_id},
        )
        self.session.delete(machine)
        self.session.commit()
