"""Encrypted reviews shared by tracked Kubernetes automation operations."""

from __future__ import annotations

import json
import uuid
from contextlib import suppress
from datetime import UTC, datetime, timedelta

from pydantic import Field
from sqlalchemy import select

from app.core.errors import ConfirmationRequiredError, InvalidStateError, NotFoundError
from app.core.secrets import SealedSecret
from app.modules.auditing import service as audit
from app.modules.cluster_lifecycle.models import Cluster, ClusterLifecycleState
from app.modules.kubernetes.custom_resource_models import CustomResourceReview
from app.modules.kubernetes.service import api_for
from app.modules.kubernetes.workflows import ConnectParams
from app.modules.organizations.identity import resolve_principal
from app.modules.organizations.models import User
from app.modules.organizations.policy import require
from app.orchestration.models import Operation
from app.orchestration.workflow import OperationCancelledError, StepFailedError

CATEGORIES = {"custom_workflow", "storage_migration", "conversion_webhook", "command_center"}


class ReviewedParams(ConnectParams):
    review_id: uuid.UUID
    step_count: int = Field(ge=1, le=20)


def persist(service, principal, cluster, category, name, payload, summary):
    sealed = service.secret_store.seal(json.dumps(payload).encode())
    row = CustomResourceReview(
        organization_id=cluster.organization_id,
        project_id=cluster.project_id,
        cluster_id=cluster.id,
        created_by=principal.subject,
        key_id=sealed.key_id,
        ciphertext=sealed.ciphertext,
        expires_at=datetime.now(UTC) + timedelta(minutes=15),
        specification={"category": category, "name": name, "summary": summary, "progress": {}, "approved": False},
    )
    service.session.add(row)
    service.session.flush()
    audit.record(
        service.session, principal=principal, action=category + ".review", resource_type="review", resource_id=row.id
    )
    service.session.commit()
    return {"id": row.id, "name": name, "expires_at": row.expires_at, **summary}


def submit(service, principal, cluster_id, review_id, confirm, category, operation_type, count):
    require(principal, "clusters.write")
    if category == "command_center":
        require(principal, "terminal.exec")
    elif category != "custom_workflow":
        require(principal, "organization.manage")
    cluster = service.cluster(principal, cluster_id)
    row = service.session.scalar(
        select(CustomResourceReview)
        .where(CustomResourceReview.id == review_id, CustomResourceReview.cluster_id == cluster_id)
        .with_for_update()
    )
    if not row or row.created_by != principal.subject or row.specification.get("category") != category:
        raise NotFoundError("Automation review not found")
    if confirm != row.specification["name"]:
        raise ConfirmationRequiredError("Repeat the reviewed target name to confirm")
    if row.specification.get("operation_id"):
        return service.operations.get(principal, uuid.UUID(row.specification["operation_id"]))
    if row.expires_at < datetime.now(UTC):
        raise InvalidStateError("Review expired; review again")
    row.specification = {**row.specification, "approved": True}
    service.session.flush()
    if category == "storage_migration" and row.specification["summary"].get("mode") == "rewrite_only":
        operation_type = "cluster.crd.rewrite"
    operation = service.operations.submit(
        principal,
        operation_type,
        {
            "cluster_id": str(cluster.id),
            "cluster_name": cluster.name,
            "review_id": str(row.id),
            "step_count": count(row),
        },
        idempotency_key=category + ":" + str(row.id),
        confirm=cluster.name,
    )
    service.session.refresh(row, with_for_update=True)
    row.specification = {**row.specification, "operation_id": str(operation.id)}
    service.session.commit()
    return operation


def access(ctx, category):
    """Reauthorize the human and the scoped target before each worker action."""
    ctx.check_cancelled()
    with ctx.db() as session:
        row = session.get(CustomResourceReview, ctx.params.review_id)
        operation = session.get(Operation, ctx.operation_id)
        cluster = session.get(Cluster, ctx.params.cluster_id)
        if (
            not row
            or not operation
            or not cluster
            or row.cluster_id != cluster.id
            or row.specification.get("category") != category
            or not row.specification.get("approved")
            or row.specification.get("operation_id") not in {None, str(ctx.operation_id)}
        ):
            raise InvalidStateError("The scoped automation review is no longer available")
        if cluster.lifecycle_state in {ClusterLifecycleState.DELETING, ClusterLifecycleState.DELETED}:
            raise InvalidStateError("The cluster is being deleted")
        user = session.get(User, operation.requested_by_id) if operation.requested_by_id else None
        if not user or not user.is_active or user.subject != row.created_by:
            raise InvalidStateError("The requesting user is no longer active")
        principal = resolve_principal(session, user, row.organization_id, row.project_id)
        require(principal, "clusters.write")
        if category == "command_center":
            require(principal, "terminal.exec")
        elif category != "custom_workflow":
            require(principal, "organization.manage")
        if (category == "command_center" or not row.specification.get("progress")) and row.expires_at < datetime.now(
            UTC
        ):
            raise InvalidStateError("Review expired before execution")
        payload = json.loads(ctx.secret_store.unseal(SealedSecret(row.key_id, row.ciphertext)))
        api = getattr(ctx, "_reviewed_api", None)
        if api is None:
            api = api_for(session, ctx.secret_store, cluster)
            ctx._reviewed_api = api
        return api, payload, dict(row.specification)


def progress(ctx, **values):
    with ctx.db() as session:
        row = session.scalar(
            select(CustomResourceReview).where(CustomResourceReview.id == ctx.params.review_id).with_for_update()
        )
        if not row:
            raise InvalidStateError("Automation review no longer exists")
        row.specification = {**row.specification, "progress": {**row.specification.get("progress", {}), **values}}


def safe_step(fn):
    def run(ctx):
        try:
            return fn(ctx)
        except OperationCancelledError:
            raise
        except Exception:
            raise StepFailedError(
                "Automation stopped: check permissions, identity, reviewed state and API readiness. "
                "Raw responses withheld."
            ) from None
        finally:
            api = getattr(ctx, "_reviewed_api", None)
            if api is not None:
                with suppress(Exception):
                    api.close()
                del ctx._reviewed_api

    return run
