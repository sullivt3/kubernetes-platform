"""Persisted scheduler and policy-approved, observable reconciliation workflows."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.core.errors import ConfirmationRequiredError, ConflictError, InvalidStateError, PlatformError
from app.core.principal import Principal
from app.modules.auditing import service as audit
from app.modules.cluster_lifecycle.models import Cluster, ClusterLifecycleState
from app.modules.gitops import service as gs
from app.modules.gitops.models import GitOpsApplication, GitOpsReview
from app.modules.gitops.policy import Policy, PolicyInput, validate
from app.modules.gitops.source import Configuration
from app.modules.kubernetes.service import api_for
from app.modules.kubernetes.workflows import ConnectParams
from app.modules.organizations.policy import require
from app.modules.organizations.scope import scope_session
from app.orchestration.models import OperationStatus
from app.orchestration.service import OperationService
from app.orchestration.workflow import (
    OperationTarget,
    StepDefinition,
    StepFailedError,
    WorkflowDefinition,
    workflow_registry,
)


class ReconcileParams(ConnectParams):
    application_id: uuid.UUID
    generation: int


def configure(service, principal, cluster_id, application_id, body: PolicyInput):
    require(principal, "organization.manage")
    _, row = gs.application(service, principal, cluster_id, application_id)
    service.session.refresh(row, with_for_update=True)
    if body.generation != row.generation:
        raise InvalidStateError("Application changed while approving policy; reload and approve again")
    if body.confirm != row.name:
        raise ConfirmationRequiredError("Repeat the application name to approve reconciliation and pruning settings")
    # Enabling/reapproving may not race an active sync. Pausing is always available.
    if body.enabled:
        gs.busy(service.session, row)
    row.generation += 1
    row.automation = {
        **Policy.model_validate(body.model_dump()).model_dump(),
        "approved_by": principal.subject,
        "approved_at": datetime.now(UTC).isoformat(),
        "approved_generation": row.generation,
    }
    row.next_reconcile_at = datetime.now(UTC) if body.enabled else None
    audit.record(
        service.session,
        principal=principal,
        action="gitops.automation.configure",
        resource_type="gitops_application",
        resource_id=row.id,
        details=Policy.model_validate(body.model_dump()).model_dump(),
    )
    service.session.commit()
    return gs.public(row)


def pause(service, principal, cluster_id, application_id):
    _, row = gs.application(service, principal, cluster_id, application_id)
    service.session.refresh(row, with_for_update=True)
    row.generation += 1
    row.automation = {**row.automation, "enabled": False}
    row.next_reconcile_at = None
    audit.record(
        service.session,
        principal=principal,
        action="gitops.automation.pause",
        resource_type="gitops_application",
        resource_id=row.id,
        details={},
    )
    service.session.commit()
    return gs.public(row)


def request_now(service, principal, cluster_id, application_id):
    _, row = gs.application(service, principal, cluster_id, application_id)
    service.session.refresh(row, with_for_update=True)
    validate(row, row.generation, automatic=True)
    gs.busy(service.session, row)
    row.next_reconcile_at = datetime.now(UTC)
    audit.record(
        service.session,
        principal=principal,
        action="gitops.automation.request",
        resource_type="gitops_application",
        resource_id=row.id,
        details={},
    )
    service.session.commit()
    return {"scheduled": True}


def prepare(ctx):
    p: ReconcileParams = ctx.params
    try:
        with ctx.db() as session:
            row = session.get(GitOpsApplication, p.application_id)
            cluster = session.get(Cluster, p.cluster_id)
            validate(row, p.generation, automatic=True)
            if (
                not cluster
                or row.cluster_id != cluster.id
                or cluster.lifecycle_state in {ClusterLifecycleState.DELETING, ClusterLifecycleState.DELETED}
            ):
                raise InvalidStateError("The target cluster is unavailable")
            # Recovery after this step committed but before its checkpoint must reuse the same review.
            existing = session.scalar(select(GitOpsReview).where(GitOpsReview.operation_id == ctx.operation_id))
            if existing:
                return {"review_id": str(existing.id), "commit": existing.commit_sha}
            policy = Policy.model_validate(row.automation)
            config = Configuration.model_validate(row.configuration)
            header = gs.auth_header(row, ctx.secret_store)
            inventory = dict(row.inventory)
            api = api_for(session, ctx.secret_store, cluster)
        with gs.GitSource(config, header, ctx) as source:
            commit, objects = source.fetch(allow_empty=True) if policy.allow_empty else source.fetch()
        # Inventory/config snapshots are plain non-secret metadata, detached from an expired session.
        from types import SimpleNamespace

        snapshot = SimpleNamespace(id=p.application_id, configuration=config.model_dump(), inventory=inventory)
        payload, summary = gs.plan_resources(api, snapshot, objects, prune=policy.prune)
        ctx.check_cancelled()
        with ctx.db() as session:
            row = session.scalar(
                select(GitOpsApplication).where(GitOpsApplication.id == p.application_id).with_for_update()
            )
            validate(row, p.generation, automatic=True)
            if row.inventory != inventory:
                raise InvalidStateError("Application inventory changed while fetching source")
            review = gs.persist_review(
                session,
                ctx.secret_store,
                row,
                "system|gitops",
                commit,
                payload,
                summary,
                automatic=True,
                operation_id=ctx.operation_id,
            )
            identifier = review.id
        ctx.info(f"Reviewed Git commit {commit} under the approved automatic policy; raw source withheld")
        return {"review_id": str(identifier), "commit": commit}
    except (PlatformError, OSError, ValueError):
        raise StepFailedError(
            "Automatic review failed: check source access, TLS, empty-source policy, Kubernetes schema and ownership. "
            "Raw output withheld."
        ) from None


def operation_status(session, operation):
    row = session.get(GitOpsApplication, uuid.UUID(operation.params["application_id"]))
    if not row or row.generation != operation.params["generation"]:
        return
    observation = {**row.observation, "reconcile_operation_id": str(operation.id)}
    if operation.status in {OperationStatus.FAILED, OperationStatus.CANCELLED}:
        observation.update(
            status=("Conflict" if observation.get("status") == "Conflict" else "Error")
            if operation.status == OperationStatus.FAILED
            else "Cancelled",
            error="Automatic reconciliation did not complete; see the operation. "
            "No field managers or finalizers were bypassed.",
        )
    row.observation = observation


workflow_registry.register(
    WorkflowDefinition(
        type="cluster.gitops.reconcile",
        title="Automatic GitOps reconciliation",
        description="Fetch and reconcile an explicitly approved source and guarded pruning policy.",
        params_model=ReconcileParams,
        destructive=True,
        plan=lambda _: [
            StepDefinition("review", "Fetch and review the approved Git source", prepare),
            StepDefinition("sync", "Reconcile resources and approved pruning", gs.execute),
        ],
        target=lambda p: OperationTarget("cluster", str(p.cluster_id), p.cluster_name),
        on_status=operation_status,
    )
)


def tick(session_factory, *, dispatcher=None, publisher=None, now=None):
    from app.orchestration.runtime import get_dispatcher, get_publisher

    now = now or datetime.now(UTC)
    with session_factory() as session:
        targets = list(
            session.execute(
                select(GitOpsApplication.id, GitOpsApplication.organization_id, GitOpsApplication.project_id)
                .where(GitOpsApplication.next_reconcile_at <= now)
                .order_by(GitOpsApplication.next_reconcile_at)
                .limit(50)
            )
        )
    report = {"queued": 0, "skipped": 0, "failed": 0}
    for application_id, organization_id, project_id in targets:
        with session_factory() as session:
            principal = replace(Principal.system(organization_id, "gitops"), project_id=project_id)
            scope_session(session, principal)
            row = session.scalar(
                select(GitOpsApplication)
                .where(GitOpsApplication.id == application_id)
                .with_for_update(skip_locked=True)
            )
            if row is None or row.next_reconcile_at is None or row.next_reconcile_at > now:
                report["skipped"] += 1
                continue
            try:
                validate(row, row.generation, automatic=True)
                cluster = session.get(Cluster, row.cluster_id)
                if not cluster or cluster.lifecycle_state in {
                    ClusterLifecycleState.DELETING,
                    ClusterLifecycleState.DELETED,
                }:
                    row.next_reconcile_at = None
                    session.commit()
                    report["skipped"] += 1
                    continue
                gs.busy(session, row)
                policy = Policy.model_validate(row.automation)
                row.next_reconcile_at = now + timedelta(seconds=policy.interval_seconds)
                service = OperationService(
                    session,
                    registry=workflow_registry,
                    dispatcher=dispatcher or get_dispatcher(),
                    publisher=publisher or get_publisher(),
                )
                operation = service.submit(
                    principal,
                    "cluster.gitops.reconcile",
                    {
                        "cluster_id": str(cluster.id),
                        "cluster_name": cluster.name,
                        "application_id": str(row.id),
                        "generation": row.generation,
                    },
                    confirm=cluster.name,
                    idempotency_key=f"gitops-auto:{row.id}:{row.generation}:{now.isoformat()}",
                )
                # Inline execution can update the observation while the submitter is running.
                session.refresh(row)
                row.observation = {**row.observation, "reconcile_operation_id": str(operation.id)}
                session.commit()
                report["queued"] += 1
            except (ConflictError, InvalidStateError):
                session.rollback()
                row = session.get(GitOpsApplication, application_id)
                if row:
                    row.next_reconcile_at = (
                        now + timedelta(seconds=30) if Policy.model_validate(row.automation).enabled else None
                    )
                    session.commit()
                report["skipped"] += 1
            except Exception:
                session.rollback()
                report["failed"] += 1
    return report
