"""Cluster lifecycle workflows. Plans are produced by the cluster's distribution."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.cluster_lifecycle.distributions.base import ClusterPlanInput
from app.modules.cluster_lifecycle.distributions.registry import distribution_registry
from app.modules.cluster_lifecycle.distributions.storage_steps import plan_storage
from app.modules.cluster_lifecycle.models import (
    Cluster,
    ClusterLifecycleState,
    ClusterMachine,
    MachineMembershipState,
)
from app.orchestration.models import Operation, OperationStatus
from app.orchestration.workflow import OperationTarget, WorkflowDefinition, workflow_registry

CLUSTER_CREATE = "cluster.create"
CLUSTER_DELETE = "cluster.delete"
CLUSTER_ADD_NODES = "cluster.nodes.add"
CLUSTER_REMOVE_NODES = "cluster.nodes.remove"
CLUSTER_UPGRADE = "cluster.upgrade"
CLUSTER_RESIZE_POOL = "cluster.pool.resize"


def _target(p: ClusterPlanInput) -> OperationTarget:
    return OperationTarget(type="cluster", id=str(p.cluster_id), name=p.cluster_name)


def _cluster(session: Session, op: Operation) -> Cluster | None:
    return session.get(Cluster, ClusterPlanInput.model_validate(op.params).cluster_id)


def _set_memberships(session: Session, cluster: Cluster, state: MachineMembershipState) -> None:
    for membership in session.scalars(select(ClusterMachine).where(ClusterMachine.cluster_id == cluster.id)):
        membership.membership_state = state


def _failure_detail(op: Operation, action: str) -> dict[str, object]:
    if op.status is OperationStatus.CANCELLED:
        return {
            "error": {"code": "cancelled", "message": f"{action} was cancelled"},
            "operation_id": str(op.id),
        }
    return {"error": op.error or {"message": f"{action} failed"}, "operation_id": str(op.id)}


def on_create_status(session: Session, op: Operation) -> None:
    cluster = _cluster(session, op)
    if cluster is None:
        return
    if op.status is OperationStatus.RUNNING:
        cluster.lifecycle_state = ClusterLifecycleState.PROVISIONING
        cluster.status_detail = {"operation_id": str(op.id)}
        _set_memberships(session, cluster, MachineMembershipState.INSTALLING)
    elif op.status is OperationStatus.SUCCEEDED:
        cluster.lifecycle_state = ClusterLifecycleState.ACTIVE
    elif op.status in (OperationStatus.FAILED, OperationStatus.CANCELLED):
        cluster.lifecycle_state = ClusterLifecycleState.ERROR
        cluster.status_detail = _failure_detail(op, "Cluster creation")
        _set_memberships(session, cluster, MachineMembershipState.FAILED)


def on_delete_status(session: Session, op: Operation) -> None:
    cluster = _cluster(session, op)
    if cluster is None:  # successfully released
        return
    if op.status is OperationStatus.RUNNING:
        cluster.lifecycle_state = ClusterLifecycleState.DELETING
        _set_memberships(session, cluster, MachineMembershipState.REMOVING)
    elif op.status in (OperationStatus.FAILED, OperationStatus.CANCELLED):
        cluster.lifecycle_state = ClusterLifecycleState.ERROR
        cluster.status_detail = _failure_detail(op, "Cluster deletion")


create_workflow = workflow_registry.register(
    WorkflowDefinition(
        type=CLUSTER_CREATE,
        title="Create cluster",
        description="Prepare machines, install Kubernetes and register the new cluster.",
        params_model=ClusterPlanInput,
        plan=lambda p: [*distribution_registry.get(p.spec.distribution).plan_create(p), *plan_storage(p)],
        target=_target,
        title_for=lambda p: f"Create cluster {p.cluster_name}",
        on_status=on_create_status,
    )
)

delete_workflow = workflow_registry.register(
    WorkflowDefinition(
        type=CLUSTER_DELETE,
        title="Delete cluster",
        description="Remove Kubernetes from every node and release the machines.",
        params_model=ClusterPlanInput,
        plan=lambda p: distribution_registry.get(p.spec.distribution).plan_delete(p),
        target=_target,
        title_for=lambda p: f"Delete cluster {p.cluster_name}",
        destructive=True,
        on_status=on_delete_status,
    )
)


def _set_node_states(session: Session, op: Operation, state: MachineMembershipState) -> None:
    plan = ClusterPlanInput.model_validate(op.params)
    ids = [n.machine_id for n in plan.nodes]
    for membership in session.scalars(
        select(ClusterMachine).where(ClusterMachine.cluster_id == plan.cluster_id, ClusterMachine.machine_id.in_(ids))
    ):
        membership.membership_state = state


def on_add_status(session: Session, op: Operation) -> None:
    # The cluster keeps serving while nodes join; only the new memberships change state.
    if op.status is OperationStatus.RUNNING:
        _set_node_states(session, op, MachineMembershipState.INSTALLING)
    elif op.status in (OperationStatus.FAILED, OperationStatus.CANCELLED):
        _set_node_states(session, op, MachineMembershipState.FAILED)


def on_remove_status(session: Session, op: Operation) -> None:
    if op.status is OperationStatus.RUNNING:
        _set_node_states(session, op, MachineMembershipState.REMOVING)
    elif op.status in (OperationStatus.FAILED, OperationStatus.CANCELLED):
        _set_node_states(session, op, MachineMembershipState.FAILED)


def on_upgrade_status(session: Session, op: Operation) -> None:
    plan = ClusterPlanInput.model_validate(op.params)
    cluster = session.get(Cluster, plan.cluster_id)
    if cluster is None:
        return
    if op.status is OperationStatus.RUNNING:
        cluster.lifecycle_state = ClusterLifecycleState.UPGRADING
        cluster.status_detail = {**(cluster.status_detail or {}), "operation_id": str(op.id)}
    elif op.status is OperationStatus.SUCCEEDED:
        target = plan.target_version
        if target:
            version = target if target.startswith("v") else f"v{target}"
            cluster.kubernetes_version = version
            spec = dict(cluster.spec or {})
            spec["kubernetes_version"] = version
            if plan.spec.distribution.value in {"k3s", "rke2"}:
                spec["options"] = {**spec.get("options", {}), "release_revision": plan.target_release_revision}
            cluster.spec = spec
        cluster.lifecycle_state = ClusterLifecycleState.ACTIVE
        cluster.status_detail = {**(cluster.status_detail or {}), "last_upgrade_operation_id": str(op.id)}
    elif op.status in (OperationStatus.FAILED, OperationStatus.CANCELLED):
        # A failed upgrade can leave nodes on mixed versions. Keep the cluster visible and
        # allow an operator to retry or repair it, while marking health as degraded.
        cluster.lifecycle_state = ClusterLifecycleState.DEGRADED
        cluster.status_detail = {**(cluster.status_detail or {}), **_failure_detail(op, "Cluster upgrade")}


add_nodes_workflow = workflow_registry.register(
    WorkflowDefinition(
        type=CLUSTER_ADD_NODES,
        title="Add nodes",
        description="Provision or prepare machines and join them to an existing cluster.",
        params_model=ClusterPlanInput,
        plan=lambda p: distribution_registry.get(p.spec.distribution).plan_add_nodes(p),
        target=_target,
        title_for=lambda p: f"Add {', '.join(n.name for n in p.nodes)} to {p.cluster_name}",
        on_status=on_add_status,
    )
)

remove_nodes_workflow = workflow_registry.register(
    WorkflowDefinition(
        type=CLUSTER_REMOVE_NODES,
        title="Remove nodes",
        description="Drain nodes, reset Kubernetes on them and release (or destroy) their machines.",
        params_model=ClusterPlanInput,
        plan=lambda p: distribution_registry.get(p.spec.distribution).plan_remove_nodes(p),
        target=_target,
        title_for=lambda p: f"Remove {', '.join(n.name for n in p.nodes)} from {p.cluster_name}",
        on_status=on_remove_status,
    )
)

upgrade_workflow = workflow_registry.register(
    WorkflowDefinition(
        type=CLUSTER_UPGRADE,
        title="Upgrade cluster",
        description="Upgrade kubeadm control planes and nodes one at a time, respecting workload disruption budgets.",
        params_model=ClusterPlanInput,
        plan=lambda p: distribution_registry.get(p.spec.distribution).plan_upgrade(p, p.target_version or ""),
        target=_target,
        title_for=lambda p: f"Upgrade {p.cluster_name} to {p.target_version}",
        on_status=on_upgrade_status,
    )
)


def _resize_steps(plan: ClusterPlanInput):
    from app.modules.cluster_lifecycle.distributions import local_steps, managed_steps
    from app.orchestration.workflow import StepDefinition

    handler = local_steps.resize if plan.spec.distribution.value == "k3d" else managed_steps.resize
    return [StepDefinition("resize", "Resize worker pool through its provider", handler)]


workflow_registry.register(
    WorkflowDefinition(
        type=CLUSTER_RESIZE_POOL,
        title="Resize worker pool",
        params_model=ClusterPlanInput,
        description="Change the desired node count of an existing managed or k3d worker pool.",
        plan=_resize_steps,
        target=_target,
        title_for=lambda p: f"Resize {p.cluster_name}/{p.pool_name} to {p.pool_count}",
    )
)
