"""Provider connections and provider-driven machine lifecycle (provision, import,
power, destroy). Every change is audited; long-running work is an operation."""

from __future__ import annotations

import re
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import (
    CapabilityNotSupportedError,
    ConfirmationRequiredError,
    ConflictError,
    NotFoundError,
    ValidationFailedError,
)
from app.core.principal import Principal
from app.modules.auditing import service as audit
from app.modules.credentials.models import Credential, CredentialKind
from app.modules.infrastructure import provider_workflows as wf
from app.modules.infrastructure.models import (
    ConnectionStatus,
    Machine,
    MachineLifecycleState,
    ProviderConnection,
)
from app.modules.infrastructure.service import HOST_RE, NAME_RE, SSH_CONNECTION_NAME, MachineService
from app.modules.infrastructure.workflows import MACHINE_INSPECT
from app.modules.providers.base import InfrastructureProvider
from app.modules.providers.registry import provider_registry
from app.modules.providers.types import ImplementationStatus, PowerAction, ProviderCapability, ProviderType
from app.orchestration.models import Operation
from app.orchestration.service import OperationService

CONNECTION_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()-]{0,126}[A-Za-z0-9)]$")
MAX_BATCH = 20


class ProviderConnectionService:
    def __init__(self, session: Session, operations: OperationService) -> None:
        self.session = session
        self.operations = operations

    # ---- connections ------------------------------------------------------------------

    def list_connections(self, principal: Principal) -> list[tuple[ProviderConnection, int]]:
        rows = self.session.execute(
            select(ProviderConnection, func.count(Machine.id))
            .outerjoin(Machine, Machine.provider_connection_id == ProviderConnection.id)
            .where(ProviderConnection.organization_id == principal.organization_id)
            .group_by(ProviderConnection.id)
            .order_by(ProviderConnection.name)
        ).all()
        return [(c, n) for c, n in rows]

    def get(self, principal: Principal, connection_id: uuid.UUID) -> ProviderConnection:
        connection = self.session.get(ProviderConnection, connection_id, populate_existing=True)
        if connection is None or connection.organization_id != principal.organization_id:
            raise NotFoundError("Provider connection not found")
        return connection

    def machine_count(self, connection_id: uuid.UUID) -> int:
        return int(
            self.session.scalar(select(func.count()).where(Machine.provider_connection_id == connection_id)) or 0
        )

    def _provider_class(self, provider_type: ProviderType) -> type[InfrastructureProvider]:
        cls = provider_registry.get_class(provider_type)
        if cls.implementation_status is ImplementationStatus.PLANNED:
            raise ValidationFailedError(
                f"The {cls.display_name} provider is not implemented yet (planned for Phase {cls.planned_phase})"
            )
        if provider_type is ProviderType.SSH:
            raise ValidationFailedError(
                "SSH servers do not need a connection; register them under Infrastructure → Machines"
            )
        return cls

    def _check_credential(
        self, principal: Principal, cls: type[InfrastructureProvider], credential_id: uuid.UUID | None
    ) -> Credential | None:
        if credential_id is None:
            if cls.credential_required:
                raise ValidationFailedError(f"{cls.display_name} needs a credential")
            return None
        credential = self.session.get(Credential, credential_id)
        if credential is None or credential.organization_id != principal.organization_id:
            raise ValidationFailedError("Credential not found")
        if cls.credential_kinds and credential.kind not in cls.credential_kinds:
            accepted = ", ".join(sorted(k.value for k in cls.credential_kinds))
            raise ValidationFailedError(f"{cls.display_name} needs a credential of kind {accepted}")
        return credential

    def create(
        self,
        principal: Principal,
        *,
        name: str,
        provider_type: ProviderType,
        config: dict[str, Any],
        credential_id: uuid.UUID | None,
    ) -> tuple[ProviderConnection, Operation]:
        name = name.strip()
        if not CONNECTION_NAME_RE.match(name) or name == SSH_CONNECTION_NAME:
            raise ValidationFailedError("Choose a name of 2-128 letters, digits, spaces and . _ ( ) -")
        cls = self._provider_class(provider_type)
        validated = provider_registry.validate_config(provider_type, config)
        credential = self._check_credential(principal, cls, credential_id)
        if self.session.scalar(
            select(func.count()).where(
                ProviderConnection.organization_id == principal.organization_id, ProviderConnection.name == name
            )
        ):
            raise ConflictError(f"A provider connection named '{name}' already exists")
        connection = ProviderConnection(
            organization_id=principal.organization_id,
            name=name,
            provider_type=provider_type,
            config=validated.model_dump(mode="json", exclude_none=True),
            credential_id=credential.id if credential else None,
            status=ConnectionStatus.UNKNOWN,
        )
        self.session.add(connection)
        self.session.flush()
        audit.record(
            self.session,
            principal=principal,
            action="provider_connection.create",
            resource_type="provider_connection",
            resource_id=connection.id,
            resource_name=name,
            details={"provider": provider_type.value, "credential": credential.name if credential else None},
        )
        self.session.commit()
        operation = self.validate(principal, connection.id)
        return self.get(principal, connection.id), operation

    def update(
        self,
        principal: Principal,
        connection_id: uuid.UUID,
        *,
        config: dict[str, Any] | None,
        credential_id: uuid.UUID | None,
        clear_credential: bool = False,
    ) -> tuple[ProviderConnection, Operation]:
        connection = self.get(principal, connection_id)
        cls = self._provider_class(connection.provider_type)
        changed: list[str] = []
        if config is not None:
            validated = provider_registry.validate_config(connection.provider_type, config)
            connection.config = validated.model_dump(mode="json", exclude_none=True)
            changed.append("config")
        if credential_id is not None or clear_credential:
            credential = self._check_credential(principal, cls, None if clear_credential else credential_id)
            connection.credential_id = credential.id if credential else None
            changed.append("credential")
        connection.status = ConnectionStatus.UNKNOWN
        audit.record(
            self.session,
            principal=principal,
            action="provider_connection.update",
            resource_type="provider_connection",
            resource_id=connection.id,
            resource_name=connection.name,
            details={"changed": changed},
        )
        self.session.commit()
        operation = self.validate(principal, connection.id)
        return self.get(principal, connection.id), operation

    def delete(self, principal: Principal, connection_id: uuid.UUID, *, confirm: str | None) -> None:
        connection = self.get(principal, connection_id)
        if connection.provider_type is ProviderType.SSH:
            raise ConflictError("The built-in SSH connection is managed by the platform")
        if confirm != connection.name:
            raise ConfirmationRequiredError(
                "Deleting a provider connection cannot be undone. Repeat its name to confirm.",
                details={"expected_confirmation": connection.name},
            )
        if self.machine_count(connection.id):
            raise ConflictError("Machines still belong to this connection; destroy or remove them first")
        audit.record(
            self.session,
            principal=principal,
            action="provider_connection.delete",
            resource_type="provider_connection",
            resource_id=connection.id,
            resource_name=connection.name,
        )
        self.session.delete(connection)
        self.session.commit()

    def _params(self, connection: ProviderConnection) -> dict[str, Any]:
        return {"connection_id": str(connection.id), "connection_name": connection.name}

    def validate(self, principal: Principal, connection_id: uuid.UUID) -> Operation:
        connection = self.get(principal, connection_id)
        cls = self._provider_class(connection.provider_type)
        self._require(cls, ProviderCapability.VALIDATE_CONNECTION)
        return self.operations.submit(principal, wf.PROVIDER_VALIDATE, self._params(connection))

    def discover(self, principal: Principal, connection_id: uuid.UUID) -> Operation:
        connection = self.get(principal, connection_id)
        self._require(self._provider_class(connection.provider_type), ProviderCapability.DISCOVER)
        return self.operations.submit(principal, wf.PROVIDER_DISCOVER, self._params(connection))

    @staticmethod
    def _require(cls: type[InfrastructureProvider], capability: ProviderCapability) -> None:
        if capability not in cls.capabilities:
            raise CapabilityNotSupportedError(
                f"{cls.display_name} does not support '{capability.value}'",
                details={"provider": cls.provider_type.value, "capability": capability.value},
            )

    # ---- machines through a provider ----------------------------------------------------

    def _ssh_key(self, principal: Principal, credential_id: uuid.UUID) -> Credential:
        credential = self.session.get(Credential, credential_id)
        if credential is None or credential.organization_id != principal.organization_id:
            raise ValidationFailedError("Credential not found")
        if credential.kind is not CredentialKind.SSH_PRIVATE_KEY:
            raise ValidationFailedError(
                "New machines are accessed with an SSH key: choose an SSH private key credential "
                "(its public key is installed on the machine)"
            )
        return credential

    def _check_new_name(self, principal: Principal, name: str) -> None:
        if not NAME_RE.match(name):
            raise ValidationFailedError(
                f"'{name}': machine names must be lowercase letters, digits and '-' (they become Kubernetes node names)"
            )
        if self.session.scalar(
            select(func.count()).where(Machine.organization_id == principal.organization_id, Machine.name == name)
        ):
            raise ConflictError(f"A machine named '{name}' already exists")

    def provision(
        self,
        principal: Principal,
        connection_id: uuid.UUID,
        *,
        names: list[str],
        cpu_cores: int,
        memory_mb: int,
        disk_gb: int,
        credential_id: uuid.UUID,
        ssh_username: str = "platform",
        network: str | None = None,
        labels: dict[str, str] | None = None,
        provider_options: dict[str, Any] | None = None,
    ) -> list[tuple[Machine, Operation]]:
        connection = self.get(principal, connection_id)
        cls = self._provider_class(connection.provider_type)
        self._require(cls, ProviderCapability.PROVISION)
        if connection.status is ConnectionStatus.ERROR:
            raise ConflictError(f"Connection '{connection.name}' failed validation: {connection.status_message}")
        if not names or len(names) > MAX_BATCH:
            raise ValidationFailedError(f"Provision between 1 and {MAX_BATCH} machines at a time")
        if len(set(names)) != len(names):
            raise ValidationFailedError("Machine names must be unique")
        credential = self._ssh_key(principal, credential_id)
        for name in names:
            self._check_new_name(principal, name)

        machines: list[Machine] = []
        for name in names:
            machine = Machine(
                organization_id=principal.organization_id,
                name=name,
                provider_connection_id=connection.id,
                external_id=f"{wf.PENDING_PREFIX}{name}",
                lifecycle_state=MachineLifecycleState.REQUESTED,
                ssh_username=ssh_username,
                access_credential_id=credential.id,
                labels=labels or {},
                provisioned=True,
                requested_spec={"cpu_cores": cpu_cores, "memory_mb": memory_mb, "disk_gb": disk_gb},
            )
            self.session.add(machine)
            machines.append(machine)
        self.session.flush()
        for machine in machines:
            audit.record(
                self.session,
                principal=principal,
                action="machine.provision",
                resource_type="machine",
                resource_id=machine.id,
                resource_name=machine.name,
                details={
                    "connection": connection.name,
                    "provider": connection.provider_type.value,
                    "cpu_cores": cpu_cores,
                    "memory_mb": memory_mb,
                    "disk_gb": disk_gb,
                    "credential": credential.name,
                },
            )
        self.session.commit()
        results = []
        for machine in machines:
            operation = self.operations.submit(
                principal,
                wf.MACHINE_PROVISION,
                {
                    "machine_id": str(machine.id),
                    "machine_name": machine.name,
                    "connection_id": str(connection.id),
                    "credential_id": str(credential.id),
                    "cpu_cores": cpu_cores,
                    "memory_mb": memory_mb,
                    "disk_gb": disk_gb,
                    "ssh_username": ssh_username,
                    "network": network,
                    "labels": labels or {},
                    "provider_options": provider_options or {},
                },
            )
            results.append((machine, operation))
        return results

    def import_machine(
        self,
        principal: Principal,
        connection_id: uuid.UUID,
        *,
        external_id: str,
        name: str,
        host: str,
        port: int,
        username: str | None,
        credential_id: uuid.UUID,
        kube_api_host: str | None = None,
        kube_api_port: int | None = None,
    ) -> tuple[Machine, Operation]:
        """Bring a machine the provider already has (found by discovery) into the inventory."""
        connection = self.get(principal, connection_id)
        cls = self._provider_class(connection.provider_type)
        self._require(cls, ProviderCapability.DISCOVER)
        if cls.category.value == "cloud":
            raise CapabilityNotSupportedError(
                "Discovered cloud instances are read-only and cannot be imported through this flow; "
                "platform-provisioned machines are registered automatically"
            )
        self._check_new_name(principal, name)
        if not HOST_RE.match(host):
            raise ValidationFailedError("Host must be a hostname or IP address")
        credential = self.session.get(Credential, credential_id)
        if credential is None or credential.organization_id != principal.organization_id:
            raise ValidationFailedError("Credential not found")
        if credential.kind not in (CredentialKind.SSH_PRIVATE_KEY, CredentialKind.SSH_PASSWORD):
            raise ValidationFailedError("Machines need an SSH private key or SSH password credential")
        if self.session.scalar(
            select(func.count()).where(
                Machine.provider_connection_id == connection.id, Machine.external_id == external_id
            )
        ):
            raise ConflictError(f"{external_id} is already in the inventory")
        metadata: dict[str, Any] = {}
        if kube_api_host and kube_api_port:
            metadata = {"kube_api_host": kube_api_host, "kube_api_port": str(kube_api_port)}
        machine = Machine(
            organization_id=principal.organization_id,
            name=name,
            provider_connection_id=connection.id,
            external_id=external_id,
            lifecycle_state=MachineLifecycleState.REQUESTED,
            ssh_host=host,
            ssh_port=port,
            ssh_username=username or credential.public_metadata.get("username") or "platform",
            access_credential_id=credential.id,
            # Only containers created with the platform's marker can be destroyed later;
            # the provider re-checks the marker before deleting anything.
            provisioned=connection.provider_type is ProviderType.LOCAL,
            provider_metadata=metadata,
        )
        self.session.add(machine)
        self.session.flush()
        audit.record(
            self.session,
            principal=principal,
            action="machine.import",
            resource_type="machine",
            resource_id=machine.id,
            resource_name=name,
            details={"connection": connection.name, "external_id": external_id, "address": f"{host}:{port}"},
        )
        self.session.commit()
        operation = self.operations.submit(
            principal, MACHINE_INSPECT, {"machine_id": str(machine.id), "machine_name": name}
        )
        return machine, operation

    def _managed_machine(self, principal: Principal, machine_id: uuid.UUID) -> tuple[Machine, ProviderConnection]:
        machine = MachineService(self.session, self.operations).get(principal, machine_id)
        connection = self.session.get(ProviderConnection, machine.provider_connection_id)
        assert connection is not None
        return machine, connection

    def power(
        self, principal: Principal, machine_id: uuid.UUID, action: PowerAction, *, confirm: str | None
    ) -> Operation:
        machine, connection = self._managed_machine(principal, machine_id)
        cls = provider_registry.get_class(connection.provider_type)
        self._require(cls, ProviderCapability.POWER)
        if machine.external_id.startswith(wf.PENDING_PREFIX):
            raise ConflictError("The machine has not been created yet")
        cluster, _role = MachineService(self.session, self.operations).assignment(machine.id)
        if action is not PowerAction.START and cluster and confirm != machine.name:
            raise ConfirmationRequiredError(
                f"{machine.name} is a node of cluster '{cluster}'; stopping or rebooting it interrupts its workloads. "
                "Repeat the machine name to confirm.",
                details={"expected_confirmation": machine.name},
            )
        audit.record(
            self.session,
            principal=principal,
            action=f"machine.power.{action.value}",
            resource_type="machine",
            resource_id=machine.id,
            resource_name=machine.name,
            details={"connection": connection.name},
        )
        self.session.commit()
        return self.operations.submit(
            principal,
            wf.MACHINE_POWER,
            {
                "machine_id": str(machine.id),
                "machine_name": machine.name,
                "connection_id": str(connection.id),
                "action": action.value,
            },
        )

    def destroy(self, principal: Principal, machine_id: uuid.UUID, *, confirm: str | None) -> Operation:
        machine, connection = self._managed_machine(principal, machine_id)
        cls = provider_registry.get_class(connection.provider_type)
        self._require(cls, ProviderCapability.DELETE)
        if not machine.provisioned:
            raise ConflictError(
                "This machine was not created by the platform, so it cannot be destroyed from here. "
                "Remove it from the inventory instead."
            )
        cluster, _role = MachineService(self.session, self.operations).assignment(machine.id)
        if cluster:
            raise ConflictError(f"Machine is a node of cluster '{cluster}'; delete the cluster first")
        if confirm != machine.name:
            raise ConfirmationRequiredError(
                "Destroying a machine deletes it and its disks at the provider. Repeat its name to confirm.",
                details={"expected_confirmation": machine.name},
            )
        audit.record(
            self.session,
            principal=principal,
            action="machine.destroy",
            resource_type="machine",
            resource_id=machine.id,
            resource_name=machine.name,
            details={"connection": connection.name, "external_id": machine.external_id},
        )
        self.session.commit()
        return self.operations.submit(
            principal,
            wf.MACHINE_DEPROVISION,
            {"machine_id": str(machine.id), "machine_name": machine.name, "connection_id": str(connection.id)},
            confirm=confirm,
        )

    def scan_network(
        self, principal: Principal, *, ranges: list[str], ports: list[int], timeout_seconds: float
    ) -> Operation:
        from app.core.config import get_settings
        from app.modules.infrastructure import discovery

        cleaned = [r.strip() for r in ranges if r.strip()]
        addresses = discovery.parse_targets(cleaned, allow_public=get_settings().discovery_allow_public_ranges)
        if len(addresses) * len(ports) > discovery.MAX_PROBES:
            raise ValidationFailedError(f"Scan too large (max {discovery.MAX_PROBES} address/port probes)")
        audit.record(
            self.session,
            principal=principal,
            action="network.discover",
            resource_type="network",
            resource_name=", ".join(cleaned)[:255],
            details={"addresses": len(addresses), "ports": ports},
        )
        self.session.commit()
        return self.operations.submit(
            principal,
            wf.NETWORK_DISCOVER,
            {"ranges": cleaned, "ports": ports, "timeout_seconds": timeout_seconds},
        )
