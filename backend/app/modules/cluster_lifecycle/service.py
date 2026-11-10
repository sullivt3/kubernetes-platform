"""Cluster lifecycle service: validation, plan preview, creation, deletion and queries."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import ConfirmationRequiredError, ConflictError, NotFoundError, ValidationFailedError
from app.core.principal import Principal
from app.modules.auditing import service as audit
from app.modules.cluster_lifecycle.distributions.base import ClusterPlanInput, PlannedNode
from app.modules.cluster_lifecycle.distributions.kubeadm import config as kubeadm_config
from app.modules.cluster_lifecycle.distributions.registry import distribution_registry
from app.modules.cluster_lifecycle.distributions.spec import ClusterSpec, MachineTemplate, NodePoolSpec, Taint
from app.modules.cluster_lifecycle.distributions.types import DistributionCapability, DistributionKind
from app.modules.cluster_lifecycle.models import (
    Cluster,
    ClusterLifecycleState,
    ClusterMachine,
    ClusterOrigin,
    MachineMembershipState,
    NodeRole,
)
from app.modules.cluster_lifecycle.workflows import (
    CLUSTER_ADD_NODES,
    CLUSTER_CREATE,
    CLUSTER_DELETE,
    CLUSTER_REMOVE_NODES,
    CLUSTER_RESIZE_POOL,
    CLUSTER_UPGRADE,
)
from app.modules.credentials.models import Credential, CredentialKind
from app.modules.infrastructure.models import ConnectionStatus, Machine, MachineLifecycleState, ProviderConnection
from app.modules.infrastructure.service import MachineService
from app.modules.providers.registry import provider_registry
from app.modules.providers.types import ImplementationStatus, ProviderCapability
from app.orchestration.models import ACTIVE_STATUSES, Operation
from app.orchestration.service import OperationService

# Written by the local provider's node image (see providers/implementations/local_node).
CONTAINER_RESOLV_CONF = "/etc/kplatform-node/resolv.conf"


@dataclass
class ValidationResult:
    problems: list[str] = field(default_factory=list)
    nodes: list[PlannedNode] = field(default_factory=list)
    steps: list[dict[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.problems


class ClusterService:
    def __init__(self, session: Session, operations: OperationService) -> None:
        self.session = session
        self.operations = operations

    # ------------------------------------------------------------------ validation

    def validate(self, principal: Principal, spec: ClusterSpec) -> ValidationResult:
        result = ValidationResult()
        distribution = distribution_registry.get(spec.distribution)
        if distribution.implementation_status is ImplementationStatus.PLANNED:
            phase = f" (planned for Phase {distribution.planned_phase})" if distribution.planned_phase else ""
            result.problems.append(f"{distribution.display_name} cluster creation is not implemented yet{phase}")
            return result
        provider_cls = provider_registry.get_class(spec.provider_type)
        if provider_cls.implementation_status is ImplementationStatus.PLANNED:
            result.problems.append(f"The {provider_cls.display_name} provider is not implemented yet")
        managed = distribution.kind in (DistributionKind.MANAGED, DistributionKind.LOCAL)
        new_machines = [] if managed else self._new_machine_names(spec)
        if managed:
            connection = (
                self.session.get(ProviderConnection, spec.provider_connection_id)
                if spec.provider_connection_id
                else None
            )
            if connection is None or connection.organization_id != principal.organization_id:
                result.problems.append("Managed Kubernetes needs a provider connection in this organization")
            elif connection.provider_type is not spec.provider_type:
                result.problems.append("The provider connection does not match the selected managed distribution")
            elif distribution.kind is DistributionKind.MANAGED and (
                ProviderCapability.MANAGED_KUBERNETES
                not in provider_registry.get_class(connection.provider_type).capabilities
            ):
                result.problems.append("The selected provider does not implement managed Kubernetes provisioning")
        if new_machines:
            result.problems.extend(self._provisioning_problems(principal, spec, [n for _, n in new_machines]))
            result.problems.extend(self._template_problems(spec))
        if distribution.kind is DistributionKind.SELF_MANAGED:
            result.notes.extend(self._container_defaults(principal, spec))
        result.problems.extend(distribution.validate_spec(spec))
        if spec.control_plane.load_balancer.value == "provider":
            connection = (
                self.session.get(ProviderConnection, spec.provider_connection_id)
                if spec.provider_connection_id
                else None
            )
            if connection is None or connection.organization_id != principal.organization_id:
                result.problems.append("Choose an AWS provider connection for the provider-managed API load balancer")
            elif connection.provider_type.value != "aws":
                result.problems.append("The provider-managed API load balancer requires an AWS connection")
            elif (
                ProviderCapability.LOAD_BALANCER
                not in provider_registry.get_class(connection.provider_type).capabilities
            ):
                result.problems.append("The selected provider does not implement load balancers")

        if self.session.scalar(
            select(Cluster.id).where(Cluster.organization_id == principal.organization_id, Cluster.name == spec.name)
        ):
            result.problems.append(f"A cluster named '{spec.name}' already exists")

        result.problems.extend(self._check_machines(principal, spec))
        if result.problems:
            return result

        result.nodes = self._planned_nodes(principal, spec) + [
            PlannedNode(
                machine_id=uuid.uuid4(),
                name=name,
                role="control_plane" if pool is spec.control_plane else "worker",
                pool=pool.name,
                provisioned=True,
            )
            for pool, name in new_machines
        ]
        if new_machines:
            result.notes.append(
                f"{len(new_machines)} machine(s) will be provisioned: {', '.join(n for _, n in new_machines)}"
            )
        preview = ClusterPlanInput(cluster_id=uuid.uuid4(), cluster_name=spec.name, spec=spec, nodes=result.nodes)
        from app.modules.cluster_lifecycle.distributions.storage_steps import plan_storage

        result.steps = [
            {"key": s.key, "name": s.name} for s in [*distribution.plan_create(preview), *plan_storage(preview)]
        ]
        return result

    def _container_defaults(self, principal: Principal, spec: ClusterSpec) -> list[str]:
        """Container-based machines (local provider) need kubelet/kube-proxy settings that real
        hosts don't: swap belongs to the container host, conntrack limits cannot be changed from
        inside a container, and Docker's embedded DNS is unusable for pods. Fill them in unless
        the user chose values explicitly."""
        ids = [m for pool in (spec.control_plane, *spec.worker_pools) for m in pool.existing_machine_ids]
        rows = (
            self.session.execute(
                select(Machine.name, ProviderConnection.provider_type)
                .join(ProviderConnection, ProviderConnection.id == Machine.provider_connection_id)
                .where(Machine.id.in_(ids), Machine.organization_id == principal.organization_id)
            ).all()
            if ids
            else []
        )
        containers = sorted(name for name, ptype in rows if provider_registry.get_class(ptype).container_machines)
        distribution = distribution_registry.get(spec.distribution)
        managed = distribution.kind in (DistributionKind.MANAGED, DistributionKind.LOCAL)
        new_machines = [] if managed else self._new_machine_names(spec)
        if new_machines and spec.provider_connection_id:
            connection = self.session.get(ProviderConnection, spec.provider_connection_id)
            if connection is not None and provider_registry.get_class(connection.provider_type).container_machines:
                containers += [name for _, name in new_machines]
        if not containers:
            return []
        defaults = {
            "allow_swap": True,
            "kube_proxy_conntrack_max_per_core": 0,
            "kubelet_resolv_conf": CONTAINER_RESOLV_CONF,
        }
        applied = [key for key, value in defaults.items() if spec.options.setdefault(key, value) == value]
        if not applied:
            return []
        return [
            f"Container-based machines ({', '.join(containers)}): applied {', '.join(applied)} "
            "(swap and conntrack belong to the container host; pods need real upstream DNS servers)"
        ]

    @staticmethod
    def _machine_name(cluster: str, pool: NodePoolSpec, index: int, control_plane: bool) -> str:
        return f"{cluster}-{'cp' if control_plane else pool.name}-{index}"[:63].rstrip("-")

    def _new_machine_names(self, spec: ClusterSpec, *, taken: set[str] | None = None) -> list[tuple[NodePoolSpec, str]]:
        """(pool, machine name) for every machine the platform must provision."""
        used = set(taken or ())
        result: list[tuple[NodePoolSpec, str]] = []
        for pool in (spec.control_plane, *spec.worker_pools):
            index = 1
            for _ in range(pool.count - len(pool.existing_machine_ids)):
                name = self._machine_name(spec.name, pool, index, pool is spec.control_plane)
                while name in used:
                    index += 1
                    name = self._machine_name(spec.name, pool, index, pool is spec.control_plane)
                used.add(name)
                result.append((pool, name))
                index += 1
        return result

    def _provisioning_problems(
        self,
        principal: Principal,
        spec: ClusterSpec,
        names: list[str],
        *,
        connection_id: uuid.UUID | None = None,
        credential_id: uuid.UUID | None = None,
        match_provider: bool = True,
    ) -> list[str]:
        problems: list[str] = []
        connection_id = connection_id or spec.provider_connection_id
        credential_id = credential_id or spec.machine_credential_id
        connection = self.session.get(ProviderConnection, connection_id) if connection_id else None
        if connection is None or connection.organization_id != principal.organization_id:
            problems.append("New machines need a provider connection to provision them (provider_connection_id)")
        else:
            cls = provider_registry.get_class(connection.provider_type)
            if match_provider and connection.provider_type is not spec.provider_type:
                problems.append("The provisioning connection does not match the selected infrastructure provider")
            if ProviderCapability.PROVISION not in cls.capabilities:
                problems.append(f"{cls.display_name} connections cannot provision machines")
            elif connection.status is ConnectionStatus.ERROR:
                problems.append(f"Connection '{connection.name}' failed validation: {connection.status_message}")
        credential = self.session.get(Credential, credential_id) if credential_id else None
        if credential is None or credential.organization_id != principal.organization_id:
            problems.append("New machines need an SSH key credential (machine_credential_id)")
        elif credential.kind is not CredentialKind.SSH_PRIVATE_KEY:
            problems.append("machine_credential_id must be an SSH private key (its public key is installed)")
        if names:
            existing = set(
                self.session.scalars(
                    select(Machine.name).where(
                        Machine.organization_id == principal.organization_id, Machine.name.in_(names)
                    )
                )
            )
            if existing:
                problems.append(f"Machine names already in use: {', '.join(sorted(existing))}")
        return problems

    def _template_problems(self, spec: ClusterSpec) -> list[str]:
        problems: list[str] = []
        connection = (
            self.session.get(ProviderConnection, spec.provider_connection_id) if spec.provider_connection_id else None
        )
        defaults = connection.config if connection else {}
        for pool in (spec.control_plane, *spec.worker_pools):
            if pool.count <= len(pool.existing_machine_ids) or pool.machine_template is None:
                continue
            template = pool.machine_template
            options = template.provider_options
            kind = spec.provider_type.value
            required = {
                "aws": ("instance_type", "subnet_id", "security_group_ids"),
                "azure": ("location", "vm_size"),
                "gcp": ("machine_type", "network"),
            }.get(kind, ())
            for key in required:
                if not options.get(key):
                    problems.append(f"Pool '{pool.name}' needs provider_options.{key}")
            if kind in {"aws", "azure", "gcp"} and not (
                template.image or options.get("image") or options.get("image_id")
            ):
                problems.append(f"Pool '{pool.name}' needs an OS image")
            if kind == "azure":
                if not (options.get("resource_group") or defaults.get("resource_group")):
                    problems.append(f"Pool '{pool.name}' needs a resource group")
                if not (options.get("subnet_id") or options.get("network_interface_id")):
                    problems.append(f"Pool '{pool.name}' needs a subnet ID or existing network interface")
                if options.get("network_interface_id") and pool.count - len(pool.existing_machine_ids) > 1:
                    problems.append("An Azure network interface can attach to only one VM; use a subnet for a pool")
            if kind == "gcp" and not (options.get("zone") or defaults.get("zones")):
                problems.append(f"Pool '{pool.name}' needs a Compute Engine zone")
        return problems

    def _provision(
        self,
        principal: Principal,
        *,
        connection_id: uuid.UUID,
        credential_id: uuid.UUID,
        username: str,
        pool: NodePoolSpec,
        template: MachineTemplate,
        names: list[str],
    ) -> list[Machine]:
        from app.modules.infrastructure.connections import ProviderConnectionService

        results = ProviderConnectionService(self.session, self.operations).provision(
            principal,
            connection_id,
            names=names,
            cpu_cores=template.cpu_cores,
            memory_mb=template.memory_mb,
            disk_gb=template.disk_gb,
            image=template.image,
            credential_id=credential_id,
            ssh_username=username,
            labels={"kplatform.io/pool": pool.name},
            provider_options=template.provider_options,
        )
        return [machine for machine, _ in results]

    def _check_machines(self, principal: Principal, spec: ClusterSpec) -> list[str]:
        ids = [m for pool in (spec.control_plane, *spec.worker_pools) for m in pool.existing_machine_ids]
        return self._check_machine_ids(principal, ids, spec.control_plane.existing_machine_ids)

    def _check_machine_ids(
        self, principal: Principal, ids: list[uuid.UUID], control_plane_ids: list[uuid.UUID]
    ) -> list[str]:
        problems: list[str] = []
        if not ids:
            return problems
        machines = {
            m.id: m
            for m in self.session.scalars(
                select(Machine).where(Machine.id.in_(ids), Machine.organization_id == principal.organization_id)
            )
        }
        assigned = dict(
            self.session.execute(
                select(ClusterMachine.machine_id, Cluster.name)
                .join(Cluster, Cluster.id == ClusterMachine.cluster_id)
                .where(ClusterMachine.machine_id.in_(ids))
            ).all()
        )
        for machine_id in ids:
            machine = machines.get(machine_id)
            if machine is None:
                problems.append(f"Machine {machine_id} is not registered")
                continue
            if machine_id in assigned:
                problems.append(f"Machine '{machine.name}' already belongs to cluster '{assigned[machine_id]}'")
            if machine.lifecycle_state is not MachineLifecycleState.READY:
                detail = f": {machine.last_error}" if machine.last_error else ""
                problems.append(
                    f"Machine '{machine.name}' is {machine.lifecycle_state.value}{detail}; inspect it until it is ready"
                )
        for machine_id in control_plane_ids:
            machine = machines.get(machine_id)
            if machine and (machine.hardware.get("cpu_cores") or 0) < 2:
                problems.append(f"Control-plane machine '{machine.name}' needs at least 2 CPUs")
        return problems

    def _planned_nodes(self, principal: Principal, spec: ClusterSpec) -> list[PlannedNode]:
        names = dict(
            self.session.execute(
                select(Machine.id, Machine.name).where(Machine.organization_id == principal.organization_id)
            ).all()
        )
        nodes = [
            PlannedNode(machine_id=m, name=names[m], role="control_plane", pool=spec.control_plane.name)
            for m in spec.control_plane.existing_machine_ids
        ]
        for pool in spec.worker_pools:
            nodes += [
                PlannedNode(machine_id=m, name=names[m], role="worker", pool=pool.name)
                for m in pool.existing_machine_ids
            ]
        return nodes

    # ------------------------------------------------------------------ commands

    def create(
        self, principal: Principal, spec: ClusterSpec, *, description: str | None = None
    ) -> tuple[Cluster, Operation]:
        validation = self.validate(principal, spec)
        if not validation.valid:
            raise ValidationFailedError(
                "The cluster specification is not valid", details={"problems": validation.problems}
            )
        distribution = distribution_registry.get(spec.distribution)
        managed = distribution.kind in (DistributionKind.MANAGED, DistributionKind.LOCAL)
        nodes = [n for n in validation.nodes if not n.provisioned]
        if distribution.kind is DistributionKind.MANAGED:
            # Provider ownership identifiers are written by the workflow, never accepted from clients.
            spec.options.pop("managed", None)
        new_machines = [] if managed else self._new_machine_names(spec)
        if new_machines:
            assert spec.provider_connection_id and spec.machine_credential_id  # checked by validate()
            for pool in (spec.control_plane, *spec.worker_pools):
                names = [name for p, name in new_machines if p is pool]
                if not names:
                    continue
                assert pool.machine_template is not None  # enforced by the spec model
                created = self._provision(
                    principal,
                    connection_id=spec.provider_connection_id,
                    credential_id=spec.machine_credential_id,
                    username=spec.machine_username,
                    pool=pool,
                    template=pool.machine_template,
                    names=names,
                )
                pool.existing_machine_ids.extend(m.id for m in created)
                role = "control_plane" if pool is spec.control_plane else "worker"
                nodes += [
                    PlannedNode(machine_id=m.id, name=m.name, role=role, pool=pool.name, provisioned=True)
                    for m in created
                ]
        if managed:
            if spec.provider_connection_id is None:
                raise ValidationFailedError("Managed cluster creation needs a provider connection")
            connection_id = spec.provider_connection_id
        else:
            connection_id = MachineService(self.session, self.operations).ensure_ssh_connection(principal).id
        cluster = Cluster(
            organization_id=principal.organization_id,
            name=spec.name,
            description=description,
            distribution=spec.distribution,
            origin=ClusterOrigin.PROVISIONED,
            lifecycle_state=ClusterLifecycleState.PENDING,
            kubernetes_version=spec.kubernetes_version if managed else None,
            provider_connection_id=connection_id,
            spec=spec.model_dump(mode="json"),
        )
        self.session.add(cluster)
        self.session.flush()
        for node in nodes:
            self.session.add(
                ClusterMachine(
                    cluster_id=cluster.id,
                    machine_id=node.machine_id,
                    role=NodeRole(node.role),
                    node_name=node.name,
                    membership_state=MachineMembershipState.PLANNED,
                )
            )
        audit.record(
            self.session,
            principal=principal,
            action="cluster.create",
            resource_type="cluster",
            resource_id=cluster.id,
            resource_name=spec.name,
            details={
                "distribution": spec.distribution.value,
                "kubernetes_version": spec.kubernetes_version,
                "nodes": [n.name for n in nodes],
                "provisioned": [n.name for n in nodes if n.provisioned],
            },
        )
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise ConflictError("A selected machine was assigned to another cluster concurrently") from exc

        plan = ClusterPlanInput(cluster_id=cluster.id, cluster_name=spec.name, spec=spec, nodes=nodes)
        operation = self.operations.submit(principal, CLUSTER_CREATE, plan.model_dump(mode="json"))
        return self.get(principal, cluster.id), operation

    def delete(
        self,
        principal: Principal,
        cluster_id: uuid.UUID,
        *,
        confirm: str | None,
        force: bool = False,
        destroy_machines: bool = False,
    ) -> Operation:
        cluster = self.get(principal, cluster_id)
        if cluster.origin is not ClusterOrigin.PROVISIONED:
            raise ValidationFailedError("Only clusters created by the platform can be deleted here")
        nodes = [
            PlannedNode(
                machine_id=m.machine_id,
                name=m.node_name or machine.name,
                role=m.role.value,
                pool="-",
                provisioned=machine.provisioned,
            )
            for m, machine in self.node_details(cluster.id)
        ]
        plan = ClusterPlanInput(
            cluster_id=cluster.id,
            cluster_name=cluster.name,
            spec=ClusterSpec.model_validate(cluster.spec),
            nodes=nodes,
            force=force,
            destroy_machines=destroy_machines,
        )
        operation = self.operations.submit(principal, CLUSTER_DELETE, plan.model_dump(mode="json"), confirm=confirm)
        audit.record(
            self.session,
            principal=principal,
            action="cluster.delete",
            resource_type="cluster",
            resource_id=cluster.id,
            resource_name=cluster.name,
            details={"force": force, "destroy_machines": destroy_machines, "operation_id": str(operation.id)},
        )
        self.session.commit()
        return operation

    # ------------------------------------------------------------------ node add / remove

    def _managed(self, principal: Principal, cluster_id: uuid.UUID, capability: DistributionCapability) -> Cluster:
        cluster = self.get(principal, cluster_id)
        if cluster.origin is not ClusterOrigin.PROVISIONED:
            raise ValidationFailedError(
                "Nodes of imported clusters are managed by whoever created the cluster; the platform can only "
                "cordon, drain, label and taint them"
            )
        if cluster.lifecycle_state not in (ClusterLifecycleState.ACTIVE, ClusterLifecycleState.DEGRADED):
            raise ConflictError(
                f"The cluster is {cluster.lifecycle_state.value}; nodes can be changed when it is active"
            )
        distribution = distribution_registry.get(cluster.distribution)
        if capability not in distribution.capabilities:
            raise ValidationFailedError(f"{distribution.display_name} clusters do not support this yet")
        if self.active_operation(cluster.id) is not None:
            raise ConflictError("Another operation is running on this cluster")
        return cluster

    def _anchor(self, cluster_id: uuid.UUID, *, exclude: frozenset[uuid.UUID] = frozenset()) -> PlannedNode:
        for membership, machine in self.node_details(cluster_id):
            if (
                membership.role is NodeRole.CONTROL_PLANE
                and membership.membership_state is MachineMembershipState.JOINED
                and membership.machine_id not in exclude
            ):
                return PlannedNode(
                    machine_id=machine.id,
                    name=membership.node_name or machine.name,
                    role="control_plane",
                    pool="-",
                )
        raise ConflictError("No joined control-plane node is available to coordinate the change")

    def add_nodes(
        self,
        principal: Principal,
        cluster_id: uuid.UUID,
        *,
        pool_name: str,
        existing_machine_ids: list[uuid.UUID],
        provision_count: int = 0,
        machine_template: MachineTemplate | None = None,
        connection_id: uuid.UUID | None = None,
        credential_id: uuid.UUID | None = None,
        labels: dict[str, str] | None = None,
        taints: list[Taint] | None = None,
    ) -> Operation:
        cluster = self._managed(principal, cluster_id, DistributionCapability.ADD_NODES)
        if distribution_registry.get(cluster.distribution).kind is not DistributionKind.SELF_MANAGED:
            raise ValidationFailedError("Use worker pool resizing for this distribution")
        spec = ClusterSpec.model_validate(cluster.spec)
        if not existing_machine_ids and provision_count < 1:
            raise ValidationFailedError("Select machines to add or a number of machines to provision")
        control_plane = pool_name == spec.control_plane.name
        if control_plane and not spec.control_plane.endpoint:
            raise ValidationFailedError(
                "This cluster was created with a single control plane and no stable API endpoint (VIP or load "
                "balancer), so more control planes cannot join. Worker nodes can be added."
            )
        pool: NodePoolSpec | None = (
            spec.control_plane if control_plane else next((p for p in spec.worker_pools if p.name == pool_name), None)
        )
        new_pool = pool is None
        if pool is None:
            pool = NodePoolSpec(name=pool_name, count=0, labels=labels or {}, taints=taints or [])
        elif labels or taints:
            raise ValidationFailedError(f"Pool '{pool_name}' exists; its labels and taints come from the pool")

        problems = self._check_machine_ids(
            principal, existing_machine_ids, existing_machine_ids if control_plane else []
        )

        # Machines to provision
        template = machine_template or pool.machine_template
        names: list[str] = []
        if provision_count:
            if template is None:
                problems.append("Give the size (CPU, memory, disk) for the machines to provision")
            taken = set(
                self.session.scalars(select(Machine.name).where(Machine.organization_id == principal.organization_id))
            )
            index = 1
            while len(names) < provision_count:
                name = self._machine_name(spec.name, pool, index, control_plane)
                if name not in taken:
                    names.append(name)
                index += 1
            problems += self._provisioning_problems(
                principal, spec, names, connection_id=connection_id, credential_id=credential_id, match_provider=False
            )
            if template is not None:
                chosen = self.session.get(ProviderConnection, connection_id or spec.provider_connection_id)
                request_spec = spec.model_copy(
                    update={
                        "provider_type": chosen.provider_type if chosen else spec.provider_type,
                        "provider_connection_id": connection_id or spec.provider_connection_id,
                        "control_plane": spec.control_plane.model_copy(
                            update={"count": len(spec.control_plane.existing_machine_ids)}
                        ),
                        "worker_pools": [
                            NodePoolSpec(name=pool.name, count=provision_count, machine_template=template)
                        ],
                    }
                )
                problems += self._template_problems(request_spec)
        if problems:
            raise ValidationFailedError("Nodes cannot be added", details={"problems": problems})

        anchor = self._anchor(cluster.id)
        role = "control_plane" if control_plane else "worker"
        machine_names = (
            dict(
                self.session.execute(select(Machine.id, Machine.name).where(Machine.id.in_(existing_machine_ids))).all()
            )
            if existing_machine_ids
            else {}
        )
        nodes = [
            PlannedNode(machine_id=m, name=machine_names[m], role=role, pool=pool.name) for m in existing_machine_ids
        ]
        if names:
            assert template is not None
            created = self._provision(
                principal,
                connection_id=(connection_id or spec.provider_connection_id),  # type: ignore[arg-type]
                credential_id=(credential_id or spec.machine_credential_id),  # type: ignore[arg-type]
                username=spec.machine_username,
                pool=pool,
                template=template,
                names=names,
            )
            nodes += [
                PlannedNode(machine_id=m.id, name=m.name, role=role, pool=pool.name, provisioned=True) for m in created
            ]

        # The desired state after the change.
        pool.existing_machine_ids = [*pool.existing_machine_ids, *(n.machine_id for n in nodes)]
        pool.count = len(pool.existing_machine_ids)
        if new_pool:
            spec.worker_pools.append(pool)
        cluster.spec = spec.model_dump(mode="json")
        for node in nodes:
            self.session.add(
                ClusterMachine(
                    cluster_id=cluster.id,
                    machine_id=node.machine_id,
                    role=NodeRole(node.role),
                    node_name=node.name,
                    membership_state=MachineMembershipState.PLANNED,
                )
            )
        audit.record(
            self.session,
            principal=principal,
            action="cluster.nodes.add",
            resource_type="cluster",
            resource_id=cluster.id,
            resource_name=cluster.name,
            details={"pool": pool.name, "nodes": [n.name for n in nodes], "provisioned": names},
        )
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise ConflictError("A selected machine was assigned to another cluster concurrently") from exc
        plan = ClusterPlanInput(cluster_id=cluster.id, cluster_name=cluster.name, spec=spec, nodes=nodes, anchor=anchor)
        return self.operations.submit(principal, CLUSTER_ADD_NODES, plan.model_dump(mode="json"))

    def remove_node(
        self,
        principal: Principal,
        cluster_id: uuid.UUID,
        machine_id: uuid.UUID,
        *,
        confirm: str | None,
        destroy_machine: bool = False,
        force: bool = False,
    ) -> Operation:
        cluster = self._managed(principal, cluster_id, DistributionCapability.REMOVE_NODES)
        details = {m.machine_id: (m, machine) for m, machine in self.node_details(cluster.id)}
        if machine_id not in details:
            raise NotFoundError("That machine is not a node of this cluster")
        membership, machine = details[machine_id]
        node_name = membership.node_name or machine.name
        if confirm != node_name:
            raise ConfirmationRequiredError(
                f"Removing {node_name} drains its workloads and resets Kubernetes on it. Repeat its name to confirm.",
                details={"expected_confirmation": node_name},
            )
        if destroy_machine and not machine.provisioned:
            raise ValidationFailedError("Only machines the platform provisioned can be destroyed")
        spec = ClusterSpec.model_validate(cluster.spec)
        control_plane = membership.role is NodeRole.CONTROL_PLANE
        if control_plane:
            remaining = [
                m for m, _ in details.values() if m.role is NodeRole.CONTROL_PLANE and m.machine_id != machine_id
            ]
            if not remaining:
                raise ConflictError("This is the only control plane; delete the cluster instead")
        anchor = self._anchor(cluster.id, exclude=frozenset({machine_id}))
        pool = (
            spec.control_plane
            if control_plane
            else next((p for p in spec.worker_pools if machine_id in p.existing_machine_ids), None)
        )
        if pool is not None:
            pool.existing_machine_ids = [m for m in pool.existing_machine_ids if m != machine_id]
            pool.count = len(pool.existing_machine_ids)
            if not control_plane and pool.count == 0:
                spec.worker_pools = [p for p in spec.worker_pools if p is not pool]
        cluster.spec = spec.model_dump(mode="json")
        node = PlannedNode(
            machine_id=machine_id,
            name=node_name,
            role=membership.role.value,
            pool=pool.name if pool else "-",
            provisioned=machine.provisioned,
        )
        audit.record(
            self.session,
            principal=principal,
            action="cluster.nodes.remove",
            resource_type="cluster",
            resource_id=cluster.id,
            resource_name=cluster.name,
            details={
                "node": node_name,
                "role": membership.role.value,
                "destroy_machine": destroy_machine,
                "force": force,
            },
        )
        self.session.commit()
        plan = ClusterPlanInput(
            cluster_id=cluster.id,
            cluster_name=cluster.name,
            spec=spec,
            nodes=[node],
            anchor=anchor,
            force=force,
            destroy_machines=destroy_machine,
        )
        return self.operations.submit(principal, CLUSTER_REMOVE_NODES, plan.model_dump(mode="json"))

    def upgrade(
        self,
        principal: Principal,
        cluster_id: uuid.UUID,
        target_version: str,
        *,
        allow_emptydir_data: bool = False,
        target_release_revision: int = 1,
    ) -> Operation:
        cluster = self._managed(principal, cluster_id, DistributionCapability.UPGRADE)
        if distribution_registry.get(cluster.distribution).kind is DistributionKind.MANAGED:
            return self._managed_upgrade(principal, cluster, target_version)
        current_text = (cluster.kubernetes_version or "").removeprefix("v").split("+", 1)[0].split("-", 1)[0]
        target_text = target_version.strip().removeprefix("v")
        try:
            current = kubeadm_config.parse_version(current_text)
            target = kubeadm_config.parse_version(target_text)
        except ValueError as exc:
            raise ValidationFailedError(str(exc)) from exc
        if target[2] is None:
            raise ValidationFailedError("Choose an exact Kubernetes patch version, for example 1.34.2")
        if current[2] is None:
            raise ValidationFailedError("The cluster's current Kubernetes patch version is unknown")
        if target[1] not in (current[1], current[1] + 1):
            raise ValidationFailedError("Upgrade one Kubernetes minor version at a time")
        revision_upgrade = (
            cluster.distribution.value in {"k3s", "rke2"}
            and target == current
            and target_release_revision > int((cluster.spec.get("options") or {}).get("release_revision", 1))
        )
        if target <= current and not revision_upgrade:
            raise ValidationFailedError("The target version must be newer than the cluster's current version")
        if target[1] < kubeadm_config.MIN_MINOR:
            raise ValidationFailedError(f"Kubernetes 1.{target[1]} is not supported by this platform")

        rows = self.node_details(cluster.id)
        if not rows:
            raise ConflictError("No managed nodes are registered for this cluster")
        if any(m.membership_state is not MachineMembershipState.JOINED for m, _ in rows):
            raise ConflictError("Every managed node must be joined before upgrading the cluster")
        spec = ClusterSpec.model_validate(cluster.spec)
        nodes = [
            PlannedNode(
                machine_id=membership.machine_id,
                name=membership.node_name or machine.name,
                role=membership.role.value,
                pool="-",
                provisioned=machine.provisioned,
            )
            for membership, machine in rows
        ]
        if not any(node.role == "control_plane" for node in nodes):
            raise ConflictError("No managed control-plane node is registered for this cluster")

        plan = ClusterPlanInput(
            cluster_id=cluster.id,
            cluster_name=cluster.name,
            spec=spec,
            nodes=nodes,
            target_version=target_text,
            target_release_revision=target_release_revision,
            allow_emptydir_data=allow_emptydir_data,
        )
        operation = self.operations.submit(principal, CLUSTER_UPGRADE, plan.model_dump(mode="json"))
        audit.record(
            self.session,
            principal=principal,
            action="cluster.upgrade",
            resource_type="cluster",
            resource_id=cluster.id,
            resource_name=cluster.name,
            details={
                "from_version": cluster.kubernetes_version,
                "target_version": f"v{target_text}",
                "allow_emptydir_data": allow_emptydir_data,
            },
        )
        self.session.commit()
        return operation

    def _managed_upgrade(self, principal: Principal, cluster: Cluster, target_version: str) -> Operation:
        try:
            observed = (cluster.kubernetes_version or "").removeprefix("v").split("+", 1)[0].split("-", 1)[0]
            current = kubeadm_config.parse_version(observed)
            target = kubeadm_config.parse_version(target_version.removeprefix("v"))
        except ValueError as exc:
            raise ValidationFailedError(str(exc)) from exc
        if cluster.distribution.value == "eks":
            if target[2] is not None:
                raise ValidationFailedError("EKS upgrades target a minor version, for example 1.35")
            if target[1] != current[1] + 1:
                raise ValidationFailedError("Upgrade EKS one minor version at a time")
        elif (
            target[2] is None
            or target[1] not in (current[1], current[1] + 1)
            or (*target[:2], target[2] or 0) <= (*current[:2], current[2] or 0)
        ):
            raise ValidationFailedError("Choose a newer exact patch in the same or next minor version")
        spec = ClusterSpec.model_validate(cluster.spec)
        plan = ClusterPlanInput(
            cluster_id=cluster.id,
            cluster_name=cluster.name,
            spec=spec,
            nodes=[],
            target_version=target_version.removeprefix("v"),
        )
        operation = self.operations.submit(principal, CLUSTER_UPGRADE, plan.model_dump(mode="json"))
        audit.record(
            self.session,
            principal=principal,
            action="cluster.upgrade",
            resource_type="cluster",
            resource_id=cluster.id,
            resource_name=cluster.name,
            details={"target_version": target_version},
        )
        self.session.commit()
        return operation

    def resize_pool(
        self, principal: Principal, cluster_id: uuid.UUID, pool_name: str, count: int, *, confirm: str | None
    ) -> Operation:
        cluster = self._managed(principal, cluster_id, DistributionCapability.ADD_NODES)
        spec = ClusterSpec.model_validate(cluster.spec)
        if (
            distribution_registry.get(cluster.distribution).kind is not DistributionKind.MANAGED
            and cluster.distribution.value != "k3d"
        ):
            raise ValidationFailedError("This distribution uses machine-based node addition and removal")
        pool = next((pool for pool in spec.worker_pools if pool.name == pool_name), None)
        if pool is None:
            raise NotFoundError("Worker pool does not exist")
        if count < 0 or count > 1000:
            raise ValidationFailedError("Node count must be between 0 and 1000")
        if count < pool.count and confirm != cluster.name:
            raise ConfirmationRequiredError(
                "Scaling down removes nodes and interrupts their workloads. Repeat the cluster name to confirm.",
                details={"expected_confirmation": cluster.name},
            )
        if spec.distribution.value == "aks" and spec.worker_pools[0].name == pool_name and count < 1:
            raise ValidationFailedError("The AKS system pool must keep at least one node")
        plan = ClusterPlanInput(
            cluster_id=cluster.id, cluster_name=cluster.name, spec=spec, nodes=[], pool_name=pool_name, pool_count=count
        )
        operation = self.operations.submit(principal, CLUSTER_RESIZE_POOL, plan.model_dump(mode="json"))
        audit.record(
            self.session,
            principal=principal,
            action="cluster.pool.resize",
            resource_type="cluster",
            resource_id=cluster.id,
            resource_name=cluster.name,
            details={"pool": pool_name, "from": pool.count, "to": count},
        )
        self.session.commit()
        return operation

    # ------------------------------------------------------------------ queries

    def list_clusters(self, principal: Principal) -> list[Cluster]:
        return list(
            self.session.scalars(
                select(Cluster).where(Cluster.organization_id == principal.organization_id).order_by(Cluster.name)
            )
        )

    def get(self, principal: Principal, cluster_id: uuid.UUID) -> Cluster:
        cluster = self.session.get(Cluster, cluster_id, populate_existing=True)
        if cluster is None or cluster.organization_id != principal.organization_id:
            raise NotFoundError("Cluster not found")
        return cluster

    def memberships(self, cluster_id: uuid.UUID) -> list[ClusterMachine]:
        return list(
            self.session.scalars(
                select(ClusterMachine)
                .where(ClusterMachine.cluster_id == cluster_id)
                .order_by(ClusterMachine.role, ClusterMachine.node_name)
            )
        )

    def node_details(self, cluster_id: uuid.UUID) -> list[tuple[ClusterMachine, Machine]]:
        rows = self.session.execute(
            select(ClusterMachine, Machine)
            .join(Machine, Machine.id == ClusterMachine.machine_id)
            .where(ClusterMachine.cluster_id == cluster_id)
            .order_by(ClusterMachine.role, ClusterMachine.node_name)
        ).all()
        return [(membership, machine) for membership, machine in rows]

    def node_counts(self, principal: Principal) -> dict[uuid.UUID, int]:
        from sqlalchemy import func

        rows = self.session.execute(
            select(ClusterMachine.cluster_id, func.count())
            .join(Cluster, Cluster.id == ClusterMachine.cluster_id)
            .where(Cluster.organization_id == principal.organization_id)
            .group_by(ClusterMachine.cluster_id)
        ).all()
        return {cluster_id: count for cluster_id, count in rows}

    def active_operation(self, cluster_id: uuid.UUID) -> Operation | None:
        return self.session.scalar(
            select(Operation)
            .where(
                Operation.target_type == "cluster",
                Operation.target_id == str(cluster_id),
                Operation.status.in_(ACTIVE_STATUSES),
            )
            .limit(1)
        )
