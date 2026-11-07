"""Cluster lifecycle service: validation, plan preview, creation, deletion and queries."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.core.principal import Principal
from app.modules.auditing import service as audit
from app.modules.cluster_lifecycle.distributions.base import ClusterPlanInput, PlannedNode
from app.modules.cluster_lifecycle.distributions.registry import distribution_registry
from app.modules.cluster_lifecycle.distributions.spec import ClusterSpec
from app.modules.cluster_lifecycle.models import (
    Cluster,
    ClusterLifecycleState,
    ClusterMachine,
    ClusterOrigin,
    MachineMembershipState,
    NodeRole,
)
from app.modules.cluster_lifecycle.workflows import CLUSTER_CREATE, CLUSTER_DELETE
from app.modules.infrastructure.models import Machine, MachineLifecycleState
from app.modules.infrastructure.service import MachineService
from app.modules.providers.registry import provider_registry
from app.modules.providers.types import ImplementationStatus, ProviderCapability
from app.orchestration.models import ACTIVE_STATUSES, Operation
from app.orchestration.service import OperationService


@dataclass
class ValidationResult:
    problems: list[str] = field(default_factory=list)
    nodes: list[PlannedNode] = field(default_factory=list)
    steps: list[dict[str, str]] = field(default_factory=list)

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
            result.problems.append(
                f"{distribution.display_name} cluster creation is not implemented yet{phase}"
            )
            return result
        provider_cls = provider_registry.get_class(spec.provider_type)
        if provider_cls.implementation_status is ImplementationStatus.PLANNED:
            result.problems.append(f"The {provider_cls.display_name} provider is not implemented yet")
        needs_provisioning = any(
            pool.count > len(pool.existing_machine_ids) for pool in (spec.control_plane, *spec.worker_pools)
        )
        if needs_provisioning and ProviderCapability.PROVISION not in provider_cls.capabilities:
            result.problems.append(
                f"{provider_cls.display_name} cannot provision new machines; "
                "select registered machines for every node"
            )
        result.problems.extend(distribution.validate_spec(spec))

        if self.session.scalar(
            select(Cluster.id).where(
                Cluster.organization_id == principal.organization_id, Cluster.name == spec.name
            )
        ):
            result.problems.append(f"A cluster named '{spec.name}' already exists")

        result.problems.extend(self._check_machines(principal, spec))
        if result.problems:
            return result

        result.nodes = self._planned_nodes(principal, spec)
        preview = ClusterPlanInput(
            cluster_id=uuid.uuid4(), cluster_name=spec.name, spec=spec, nodes=result.nodes
        )
        result.steps = [{"key": s.key, "name": s.name} for s in distribution.plan_create(preview)]
        return result

    def _check_machines(self, principal: Principal, spec: ClusterSpec) -> list[str]:
        problems: list[str] = []
        ids = [m for pool in (spec.control_plane, *spec.worker_pools) for m in pool.existing_machine_ids]
        if not ids:
            return problems
        machines = {
            m.id: m
            for m in self.session.scalars(
                select(Machine).where(
                    Machine.id.in_(ids), Machine.organization_id == principal.organization_id
                )
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
                problems.append(
                    f"Machine '{machine.name}' already belongs to cluster '{assigned[machine_id]}'"
                )
            if machine.lifecycle_state is not MachineLifecycleState.READY:
                detail = f": {machine.last_error}" if machine.last_error else ""
                problems.append(
                    f"Machine '{machine.name}' is {machine.lifecycle_state.value}{detail}; "
                    "inspect it until it is ready"
                )
        for machine_id in spec.control_plane.existing_machine_ids:
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
        connection = MachineService(self.session, self.operations).ensure_ssh_connection(principal)
        cluster = Cluster(
            organization_id=principal.organization_id,
            name=spec.name,
            description=description,
            distribution=spec.distribution,
            origin=ClusterOrigin.PROVISIONED,
            lifecycle_state=ClusterLifecycleState.PENDING,
            kubernetes_version=None,
            provider_connection_id=connection.id,
            spec=spec.model_dump(mode="json"),
        )
        self.session.add(cluster)
        self.session.flush()
        for node in validation.nodes:
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
                "nodes": [n.name for n in validation.nodes],
            },
        )
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise ConflictError("A selected machine was assigned to another cluster concurrently") from exc

        plan = ClusterPlanInput(
            cluster_id=cluster.id, cluster_name=spec.name, spec=spec, nodes=validation.nodes
        )
        operation = self.operations.submit(principal, CLUSTER_CREATE, plan.model_dump(mode="json"))
        return self.get(principal, cluster.id), operation

    def delete(
        self, principal: Principal, cluster_id: uuid.UUID, *, confirm: str | None, force: bool = False
    ) -> Operation:
        cluster = self.get(principal, cluster_id)
        if cluster.origin is not ClusterOrigin.PROVISIONED:
            raise ValidationFailedError("Only clusters created by the platform can be deleted here")
        nodes = [
            PlannedNode(
                machine_id=m.machine_id, name=m.node_name or str(m.machine_id), role=m.role.value, pool="-"
            )
            for m in self.memberships(cluster.id)
        ]
        plan = ClusterPlanInput(
            cluster_id=cluster.id,
            cluster_name=cluster.name,
            spec=ClusterSpec.model_validate(cluster.spec),
            nodes=nodes,
            force=force,
        )
        operation = self.operations.submit(
            principal, CLUSTER_DELETE, plan.model_dump(mode="json"), confirm=confirm
        )
        audit.record(
            self.session,
            principal=principal,
            action="cluster.delete",
            resource_type="cluster",
            resource_id=cluster.id,
            resource_name=cluster.name,
            details={"force": force, "operation_id": str(operation.id)},
        )
        self.session.commit()
        return operation

    # ------------------------------------------------------------------ queries

    def list_clusters(self, principal: Principal) -> list[Cluster]:
        return list(
            self.session.scalars(
                select(Cluster)
                .where(Cluster.organization_id == principal.organization_id)
                .order_by(Cluster.name)
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
