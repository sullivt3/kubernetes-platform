"""Explicit namespace grants; no implicit interactive authority from cluster write."""

import re
import uuid

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from app.core.errors import InvalidStateError, NotFoundError, PermissionDeniedError
from app.modules.auditing import service as audit
from app.modules.command_center.session_models import CommandNamespaceGrant, CommandSession
from app.modules.kubernetes.custom_resources import request
from app.modules.kubernetes.service import api_for
from app.modules.organizations.identity import resolve_principal
from app.modules.organizations.models import User
from app.modules.organizations.policy import permissions, require

RULES = [
    {
        "apiGroups": [""],
        "resources": ["pods", "services", "events", "persistentvolumeclaims", "resourcequotas", "limitranges"],
        "verbs": ["get", "list", "watch"],
    },
    {
        "apiGroups": ["apps"],
        "resources": ["deployments", "replicasets", "statefulsets", "daemonsets"],
        "verbs": ["get", "list", "watch"],
    },
    {"apiGroups": ["batch"], "resources": ["jobs", "cronjobs"], "verbs": ["get", "list", "watch"]},
]


class Assignment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: uuid.UUID
    namespace: str


def is_admin(principal):
    return "platform.manage" in permissions(principal)


def namespace_allowed(name):
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", name) or name.startswith(("kube-", "kp-shell-")):
        raise InvalidStateError("Choose an existing application namespace")


def assignments(service, principal, cluster_id):
    require(principal, "platform.manage")
    service.cluster(principal, cluster_id)
    return [
        {"id": r.id, "user_id": r.user_id, "namespace": r.namespace}
        for r in service.session.scalars(
            select(CommandNamespaceGrant).where(CommandNamespaceGrant.cluster_id == cluster_id)
        )
    ]


def assign(service, principal, cluster_id, body):
    require(principal, "platform.manage")
    cluster = service.cluster(principal, cluster_id)
    namespace_allowed(body.namespace)
    user = service.session.get(User, body.user_id)
    if not user or not user.is_active:
        raise NotFoundError("Active platform user not found")
    subject = resolve_principal(service.session, user, cluster.organization_id, cluster.project_id)
    require(subject, "terminal.exec")
    existing = service.session.scalar(
        select(CommandNamespaceGrant).where(
            CommandNamespaceGrant.cluster_id == cluster_id,
            CommandNamespaceGrant.user_id == user.id,
            CommandNamespaceGrant.namespace == body.namespace,
        )
    )
    if existing:
        raise InvalidStateError("Revoke the existing assignment before replacing it")
    api = api_for(service.session, service.secret_store, cluster)
    try:
        ns = request(api, "GET", "/api/v1/namespaces/" + body.namespace)
    finally:
        api.close()
    if ns["metadata"].get("deletionTimestamp"):
        raise InvalidStateError("Namespace is terminating")
    row = CommandNamespaceGrant(
        organization_id=cluster.organization_id,
        project_id=cluster.project_id,
        cluster_id=cluster_id,
        user_id=user.id,
        namespace=body.namespace,
        namespace_uid=ns["metadata"]["uid"],
    )
    service.session.add(row)
    service.session.flush()
    audit.record(
        service.session,
        principal=principal,
        action="command_session.namespace_granted",
        resource_type="command_namespace_grant",
        resource_id=row.id,
        details={"cluster_id": str(cluster_id), "user_id": str(user.id), "namespace": body.namespace},
    )
    service.session.commit()
    return {"id": row.id, "user_id": row.user_id, "namespace": row.namespace}


def revoke(service, principal, cluster_id, grant_id):
    from app.modules.command_center import sessions

    require(principal, "platform.manage")
    service.cluster(principal, cluster_id)
    row = service.session.get(CommandNamespaceGrant, grant_id)
    if not row or row.cluster_id != cluster_id:
        raise NotFoundError("Namespace assignment not found")
    affected = list(
        service.session.scalars(
            select(CommandSession).where(
                CommandSession.cluster_id == cluster_id,
                CommandSession.status != "closed",
                CommandSession.specification["grant_id"].as_string() == str(row.id),
            )
        )
    )
    # Commit revocation and Closing leases together. Beat can recover a lost enqueue.
    for session in affected:
        session.status = "closing"
        session.specification = {**session.specification, "outcome": "namespace_assignment_revoked"}
    audit.record(
        service.session,
        principal=principal,
        action="command_session.namespace_revoked",
        resource_type="command_namespace_grant",
        resource_id=row.id,
        details={"cluster_id": str(cluster_id)},
    )
    service.session.delete(row)
    service.session.commit()
    for session in affected:
        sessions.enqueue_cleanup(session.id)


def authorize(db, principal, row):
    for permission in ("terminal.exec", "clusters.write"):
        require(principal, permission)
    if row.specification.get("profile", "admin") == "admin":
        require(principal, "platform.manage")
        require(principal, "secrets.reveal")
        return
    grant = db.get(CommandNamespaceGrant, uuid.UUID(row.specification["grant_id"]))
    if (
        not grant
        or grant.user_id != principal.user_id
        or grant.cluster_id != row.cluster_id
        or grant.organization_id != principal.organization_id
        or grant.project_id != row.project_id
        or grant.namespace != row.specification["target_namespace"]
        or grant.namespace_uid != row.specification["target_namespace_uid"]
    ):
        raise PermissionDeniedError("Interactive namespace assignment changed or was revoked")
