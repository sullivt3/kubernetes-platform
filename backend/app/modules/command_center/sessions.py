"""Disposable, admin-only cluster runtimes with durable leases and owned cleanup."""

from __future__ import annotations

import base64
import copy
import ipaddress
import json
import re
import time
import uuid
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta

import yaml
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import DateTime, and_, or_, select, text

from app.core.config import get_settings
from app.core.errors import ConfirmationRequiredError, InvalidStateError, NotFoundError
from app.core.principal import Principal
from app.core.secrets import get_secret_store
from app.db.session import get_engine, get_session_factory
from app.modules.auditing import service as audit
from app.modules.cluster_lifecycle.models import Cluster, ClusterLifecycleState
from app.modules.command_center.session_models import CommandSession
from app.modules.kubernetes.custom_resources import request
from app.modules.kubernetes.service import api_for, connection_target
from app.modules.kubernetes.workflows import ConnectParams
from app.modules.organizations.identity import resolve_principal
from app.modules.organizations.models import User
from app.modules.organizations.policy import require
from app.modules.organizations.scope import scope_session
from app.orchestration.models import Operation, OperationStatus
from app.orchestration.workflow import (
    OperationCancelledError,
    OperationTarget,
    StepDefinition,
    StepFailedError,
    WorkflowDefinition,
    workflow_registry,
)

LABEL = "kplatform.io/command-session"
LIFETIME = 600
ATTACH_SECONDS = 60
HEARTBEAT_SECONDS = 20


def now():
    return datetime.now(UTC)


def permissions(principal):
    for permission in ("platform.manage", "terminal.exec", "clusters.write", "secrets.reveal"):
        require(principal, permission)


class Start(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: str
    acknowledge_cluster_admin: bool = Field(strict=True)


class SessionParams(ConnectParams):
    session_id: uuid.UUID


def namespace(row):
    return "kp-shell-" + row.id.hex


def binding_path(row):
    return "/apis/rbac.authorization.k8s.io/v1/clusterrolebindings/" + namespace(row)


def ns_path(row):
    return "/api/v1/namespaces/" + namespace(row)


def public(row):
    return {
        "id": row.id,
        "status": row.status,
        "created_at": row.created_at,
        "expires_at": row.expires_at,
        "namespace": namespace(row),
        "operation_id": row.operation_id,
        "outcome": row.specification.get("outcome"),
        "image": row.specification["image"],
    }


def list_sessions(service, principal, cluster_id):
    permissions(principal)
    cluster = service.cluster(principal, cluster_id)
    return {
        "cluster_name": cluster.name,
        "enabled": bool(get_settings().command_center_runtime_image),
        "lifetime_seconds": LIFETIME,
        "items": [
            public(r)
            for r in service.session.scalars(
                select(CommandSession)
                .where(CommandSession.cluster_id == cluster_id, CommandSession.created_by == principal.subject)
                .order_by(CommandSession.created_at.desc())
                .limit(20)
            )
        ],
    }


def find(service, principal, cluster_id, session_id):
    permissions(principal)
    service.cluster(principal, cluster_id)
    row = service.session.scalar(
        select(CommandSession).where(
            CommandSession.id == session_id,
            CommandSession.cluster_id == cluster_id,
            CommandSession.created_by == principal.subject,
        )
    )
    if not row:
        raise NotFoundError("Interactive session not found")
    return row


def start(service, principal, cluster_id, body):
    permissions(principal)
    cluster = service.cluster(principal, cluster_id)
    if body.confirm != cluster.name or body.acknowledge_cluster_admin is not True:
        raise ConfirmationRequiredError("Acknowledge full cluster access and repeat the cluster name")
    settings = get_settings()
    if not settings.command_center_runtime_image:
        raise InvalidStateError("Configure a trusted digest-pinned COMMAND_CENTER_RUNTIME_IMAGE first")
    version = re.fullmatch(r"v?(\d+)\.(\d+).*", cluster.kubernetes_version or "")
    if not version or int(version[1]) != 1 or abs(int(version[2]) - settings.command_center_kubectl_minor) > 1:
        raise InvalidStateError("Runtime kubectl must be within one minor version of the observed cluster version")
    service.session.refresh(cluster, with_for_update=True)
    if cluster.lifecycle_state in {ClusterLifecycleState.DELETED, ClusterLifecycleState.DELETING}:
        raise InvalidStateError("Cluster is being deleted")
    deleting = service.session.scalar(
        select(Operation.id)
        .where(
            Operation.target_type == "cluster",
            Operation.target_id == str(cluster.id),
            Operation.type == "cluster.delete",
            Operation.status.in_(
                [OperationStatus.PENDING, OperationStatus.QUEUED, OperationStatus.RUNNING, OperationStatus.CANCELLING]
            ),
        )
        .limit(1)
    )
    if deleting:
        raise InvalidStateError("Cluster deletion is queued or running")
    active = list(
        service.session.scalars(
            select(CommandSession).where(CommandSession.cluster_id == cluster_id, CommandSession.status != "closed")
        )
    )
    if len(active) >= 4 or any(r.created_by == principal.subject for r in active):
        raise InvalidStateError("Close your existing session first; at most four sessions per cluster are allowed")
    row = CommandSession(
        organization_id=cluster.organization_id,
        project_id=cluster.project_id,
        cluster_id=cluster.id,
        created_by=principal.subject,
        status="starting",
        expires_at=now() + timedelta(seconds=LIFETIME),
        specification={
            "image": settings.command_center_runtime_image,
            "uids": {},
            "context_project_id": str(principal.project_id) if principal.project_id else None,
        },
    )
    service.session.add(row)
    service.session.flush()
    audit.record(
        service.session,
        principal=principal,
        action="command_session.create",
        resource_type="command_session",
        resource_id=row.id,
        details={"cluster_id": str(cluster.id), "privilege": "cluster-admin"},
    )
    service.session.commit()
    op = service.operations.submit(
        principal,
        "cluster.command.session.prepare",
        {"cluster_id": str(cluster.id), "cluster_name": cluster.name, "session_id": str(row.id)},
        confirm=cluster.name,
        idempotency_key="command-session:" + str(row.id),
    )
    service.session.refresh(row)
    row.operation_id = op.id
    service.session.commit()
    return public(row)


def get_row(session, session_id, *, lock=False):
    q = select(CommandSession).where(CommandSession.id == session_id)
    row = session.scalar(q.with_for_update() if lock else q)
    if not row:
        raise NotFoundError("Interactive session not found")
    return row


def guard(ctx):
    ctx.check_cancelled()
    with ctx.db() as session:
        row = get_row(session, ctx.params.session_id)
        operation = session.get(Operation, ctx.operation_id)
        cluster = session.get(Cluster, row.cluster_id)
        user = session.get(User, operation.requested_by_id) if operation and operation.requested_by_id else None
        if (
            not cluster
            or not user
            or not user.is_active
            or user.subject != row.created_by
            or row.cluster_id != ctx.params.cluster_id
            or row.operation_id not in {None, ctx.operation_id}
            or row.status != "starting"
            or row.expires_at <= now()
            or cluster.lifecycle_state in {ClusterLifecycleState.DELETING, ClusterLifecycleState.DELETED}
        ):
            raise InvalidStateError("Session lease, target or requesting user changed")
        permissions(resolve_principal(session, user, row.organization_id, row.project_id))
        return row.id


def owned(obj, row, key):
    meta = obj.get("metadata") or {}
    if meta.get("labels", {}).get(LABEL) != str(row.id):
        raise InvalidStateError("Session resource ownership differs")
    expected = row.specification.get("uids", {}).get(key)
    if expected and meta.get("uid") != expected:
        raise InvalidStateError("Session resource identity changed")
    return obj


def remember(session_id, key, obj):
    with get_session_factory()() as session:
        row = get_row(session, session_id, lock=True)
        row.specification = {**row.specification, "uids": {**row.specification["uids"], key: obj["metadata"]["uid"]}}
        session.commit()


def ensure(api, row, collection, obj, key):
    obj = copy.deepcopy(obj)
    obj.setdefault("metadata", {}).setdefault("labels", {})[LABEL] = str(row.id)
    path = collection + "/" + obj["metadata"]["name"]
    status, raw = api.request("GET", path)
    if status == 404:
        result = request(api, "POST", collection, body=obj)
    elif status == 200:
        result = owned(json.loads(raw), row, key)
        # Retry only namespace/SA/binding/policy/pod creation; never replace or adopt.
        # A previously issued credential is not reissued after worker interruption.
        if key in {"secret", "pod"}:
            raise InvalidStateError("Runtime already created; start a new session")
    else:
        raise InvalidStateError("Cannot inspect session resource")
    owned(result, row, key)
    remember(row.id, key, result)
    row.specification = {**row.specification, "uids": {**row.specification["uids"], key: result["metadata"]["uid"]}}
    return result


def api_egress(api):
    service = request(api, "GET", "/api/v1/namespaces/default/services/kubernetes")
    address = str(ipaddress.ip_address(service["spec"]["clusterIP"]))
    endpoints = request(api, "GET", "/api/v1/namespaces/default/endpoints/kubernetes")
    rules = [
        {
            "to": [{"ipBlock": {"cidr": address + ("/128" if ":" in address else "/32")}}],
            "ports": [{"protocol": "TCP", "port": 443}],
        }
    ]
    for subset in endpoints.get("subsets", []):
        for endpoint in subset.get("addresses", []):
            host = str(ipaddress.ip_address(endpoint["ip"]))
            for port in subset.get("ports", []):
                if type(port.get("port")) is not int or not 1 <= port["port"] <= 65535:
                    raise InvalidStateError("Invalid API endpoint port")
                rules.append(
                    {
                        "to": [{"ipBlock": {"cidr": host + ("/128" if ":" in host else "/32")}}],
                        "ports": [{"protocol": "TCP", "port": port["port"]}],
                    }
                )
    if len(rules) < 2 or len(rules) > 32:
        raise InvalidStateError("API endpoint inventory is incomplete or oversized")
    return address, rules


def provision(ctx):
    guard(ctx)
    with ctx.db() as session:
        row = get_row(session, ctx.params.session_id)
        cluster = session.get(Cluster, row.cluster_id)
        target = connection_target(session, ctx.secret_store, cluster)
        api = api_for(session, ctx.secret_store, cluster, allow_command_sessions=True)
        # Materialize row outside its transaction, never credentials into DB fields.
    try:
        with get_session_factory()() as session:
            row = get_row(session, ctx.params.session_id)
            session.expunge(row)
        name = namespace(row)
        root = ns_path(row)
        system = request(api, "GET", "/api/v1/namespaces/kube-system")
        cluster_uid = system["metadata"]["uid"]
        with ctx.db() as session:
            record = get_row(session, row.id, lock=True)
            record.specification = {**record.specification, "cluster_uid": cluster_uid}
        row.specification = {**row.specification, "cluster_uid": cluster_uid}
        address, egress = api_egress(api)
        guard(ctx)
        ensure(
            api,
            row,
            "/api/v1/namespaces",
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {"name": name, "labels": {"pod-security.kubernetes.io/enforce": "restricted"}},
            },
            "namespace",
        )
        guard(ctx)
        ensure(
            api,
            row,
            root + "/serviceaccounts",
            {
                "apiVersion": "v1",
                "kind": "ServiceAccount",
                "metadata": {"name": "session"},
                "automountServiceAccountToken": False,
            },
            "account",
        )
        guard(ctx)
        ensure(
            api,
            row,
            "/apis/rbac.authorization.k8s.io/v1/clusterrolebindings",
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "ClusterRoleBinding",
                "metadata": {"name": name},
                "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "cluster-admin"},
                "subjects": [{"kind": "ServiceAccount", "name": "session", "namespace": name}],
            },
            "binding",
        )
        ensure(
            api,
            row,
            "/apis/networking.k8s.io/v1/namespaces/" + name + "/networkpolicies",
            {
                "apiVersion": "networking.k8s.io/v1",
                "kind": "NetworkPolicy",
                "metadata": {"name": "api-only"},
                "spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"], "ingress": [], "egress": egress},
            },
            "networkpolicy",
        )
        guard(ctx)
        pod = ensure(
            api,
            row,
            root + "/pods",
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {"name": "runtime"},
                "spec": {
                    "restartPolicy": "Never",
                    "activeDeadlineSeconds": LIFETIME,
                    "terminationGracePeriodSeconds": 1,
                    "serviceAccountName": "session",
                    "automountServiceAccountToken": False,
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 65532,
                        "runAsGroup": 65532,
                        "fsGroup": 65532,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [
                        {
                            "name": "tools",
                            "image": row.specification["image"],
                            "imagePullPolicy": "IfNotPresent",
                            "command": ["/bin/sh", "-c", "sleep 600"],
                            "securityContext": {
                                "readOnlyRootFilesystem": True,
                                "allowPrivilegeEscalation": False,
                                "capabilities": {"drop": ["ALL"]},
                            },
                            "resources": {
                                "requests": {"cpu": "50m", "memory": "64Mi"},
                                "limits": {"cpu": "500m", "memory": "256Mi", "ephemeral-storage": "64Mi"},
                            },
                            "volumeMounts": [
                                {"name": "credentials", "mountPath": "/credentials", "readOnly": True},
                                {"name": "home", "mountPath": "/home/session"},
                                {"name": "tmp", "mountPath": "/tmp"},
                            ],
                        }
                    ],
                    "volumes": [
                        {"name": "credentials", "secret": {"secretName": "credentials", "defaultMode": 288}},
                        {"name": "home", "emptyDir": {"medium": "Memory", "sizeLimit": "32Mi"}},
                        {"name": "tmp", "emptyDir": {"medium": "Memory", "sizeLimit": "16Mi"}},
                    ],
                },
            },
            "pod",
        )
        guard(ctx)
        issued = request(
            api,
            "POST",
            root + "/serviceaccounts/session/token",
            body={
                "apiVersion": "authentication.k8s.io/v1",
                "kind": "TokenRequest",
                "spec": {
                    "expirationSeconds": LIFETIME,
                    "boundObjectRef": {
                        "apiVersion": "v1",
                        "kind": "Pod",
                        "name": "runtime",
                        "uid": pod["metadata"]["uid"],
                    },
                },
            },
        )
        expires = datetime.fromisoformat(issued["status"]["expirationTimestamp"].replace("Z", "+00:00"))
        if not now() + timedelta(seconds=30) < expires <= now() + timedelta(seconds=LIFETIME + 5):
            raise InvalidStateError("API server did not honor the short-lived token limit")
        host = "[" + address + "]" if ":" in address else address
        config = {
            "apiVersion": "v1",
            "kind": "Config",
            "current-context": "session",
            "clusters": [
                {
                    "name": "cluster",
                    "cluster": {
                        "server": "https://" + host + ":443",
                        "tls-server-name": "kubernetes.default.svc",
                        "certificate-authority-data": base64.b64encode(target.kubeconfig.ca_pem.encode()).decode(),
                    },
                }
            ],
            "users": [{"name": "session", "user": {"token": issued["status"]["token"]}}],
            "contexts": [
                {"name": "session", "context": {"cluster": "cluster", "user": "session", "namespace": "default"}}
            ],
        }
        guard(ctx)
        ensure(
            api,
            row,
            root + "/secrets",
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": "credentials"},
                "type": "Opaque",
                "data": {"kubeconfig": base64.b64encode(yaml.safe_dump(config).encode()).decode()},
            },
            "secret",
        )
        del issued, config
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            guard(ctx)
            pod = owned(request(api, "GET", root + "/pods/runtime"), row, "pod")
            if pod.get("status", {}).get("phase") in {"Failed", "Succeeded"}:
                raise InvalidStateError("Runtime pod terminated before attachment")
            if any(
                c.get("type") == "Ready" and c.get("status") == "True"
                for c in pod.get("status", {}).get("conditions", [])
            ):
                break
            time.sleep(1)
        else:
            raise InvalidStateError("Runtime pod did not become Ready")
        guard(ctx)
        with ctx.db() as session:
            record = get_row(session, row.id, lock=True)
            if record.status != "starting":
                raise InvalidStateError("Session was cancelled")
            record.status = "ready"
            record.specification = {
                **record.specification,
                "attach_until": (now() + timedelta(seconds=ATTACH_SECONDS)).isoformat(),
                "token_expires_at": expires.isoformat(),
            }
            audit.record(
                session,
                principal=Principal.system(record.organization_id, "command-sessions"),
                action="command_session.ready",
                resource_type="command_session",
                resource_id=record.id,
            )
        ctx.set_result({"session_id": str(row.id), "status": "ready"})
        return {"session_id": str(row.id), "status": "ready"}
    except BaseException:
        mark_closing(ctx.params.session_id, "prepare_stopped")
        raise
    finally:
        api.close()


@contextmanager
def resource_lock(session_id):
    # Serialize provisioning/cleanup across API and worker replicas. API close sets
    # the durable closing flag immediately; it never waits on this lock.
    key = int.from_bytes(session_id.bytes[:8], "big", signed=True)
    with get_engine().connect() as connection:
        acquired = connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
        try:
            yield bool(acquired)
        finally:
            if acquired:
                try:
                    connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                except Exception:
                    connection.invalidate()


def prepare(ctx):
    try:
        with resource_lock(ctx.params.session_id) as acquired:
            if not acquired:
                raise InvalidStateError("Runtime is already being provisioned or cleaned")
            return provision(ctx)
    except OperationCancelledError:
        raise
    except Exception:
        raise StepFailedError(
            "Interactive runtime preparation failed; inspect readiness, permissions and runtime image. "
            "Raw data withheld."
        ) from None
    finally:
        with get_session_factory()() as session:
            row = session.get(CommandSession, ctx.params.session_id)
            closing = row and row.status == "closing"
        if closing:
            cleanup(ctx.params.session_id)


def due(row, stamp):
    if row.status == "closed":
        return False
    if row.status == "closing" or row.expires_at <= stamp:
        return True
    if row.status == "active":
        return not row.heartbeat_at or row.heartbeat_at < stamp - timedelta(seconds=HEARTBEAT_SECONDS)
    if row.status == "starting":
        return row.created_at < stamp - timedelta(seconds=120)
    if row.status == "ready":
        return datetime.fromisoformat(row.specification["attach_until"]) <= stamp
    return False


def mark_closing(session_id, outcome, only_if_due=False):
    with get_session_factory()() as session:
        row = session.scalar(select(CommandSession).where(CommandSession.id == session_id).with_for_update())
        if not row or row.status == "closed" or (only_if_due and not due(row, now())):
            return False
        if row.status != "closing":
            audit.record(
                session,
                principal=Principal.system(row.organization_id, "command-sessions"),
                action="command_session.end_requested",
                resource_type="command_session",
                resource_id=row.id,
                details={"outcome": outcome, "created_by": row.created_by},
            )
        row.status = "closing"
        row.specification = {**row.specification, "outcome": row.specification.get("outcome", outcome)}
        session.commit()
        return True


def cleanup_owned(session_id):
    """Revoke only owned grants, then delete only the owned namespace; retain pending cleanup."""
    with get_session_factory()() as session:
        row = session.get(CommandSession, session_id)
        if not row or row.status == "closed":
            return True
        session.expunge(row)
        cluster = session.get(Cluster, row.cluster_id)
        if not cluster:
            return False
        try:
            api = api_for(session, get_secret_store(), cluster, allow_command_sessions=True)
        except Exception:
            return False
    try:
        if row.specification.get("cluster_uid"):
            system = request(api, "GET", "/api/v1/namespaces/kube-system")
            if system["metadata"]["uid"] != row.specification["cluster_uid"]:
                return False
        # Revoke subjects before deletion, so a finalizer cannot retain the grant.
        path = binding_path(row)
        status, raw = api.request("GET", path)
        if status == 200:
            binding = owned(json.loads(raw), row, "binding")
            if binding.get("subjects"):
                binding["subjects"] = []
                binding = request(api, "PUT", path, body=binding)
            request(
                api,
                "DELETE",
                path,
                body={
                    "apiVersion": "v1",
                    "kind": "DeleteOptions",
                    "preconditions": {
                        "uid": binding["metadata"]["uid"],
                        "resourceVersion": binding["metadata"]["resourceVersion"],
                    },
                },
            )
            status, _ = api.request("GET", path)
        if status != 404:
            return False
        status, raw = api.request("GET", ns_path(row))
        if status == 200:
            obj = owned(json.loads(raw), row, "namespace")
            request(
                api,
                "DELETE",
                ns_path(row),
                body={
                    "apiVersion": "v1",
                    "kind": "DeleteOptions",
                    "preconditions": {"uid": obj["metadata"]["uid"]},
                    "propagationPolicy": "Foreground",
                },
            )
            status, _ = api.request("GET", ns_path(row))
        if status != 404:
            return False
        with get_session_factory()() as session:
            record = get_row(session, row.id, lock=True)
            record.status = "closed"
            audit.record(
                session,
                principal=Principal.system(row.organization_id, "command-sessions"),
                action="command_session.closed",
                resource_type="command_session",
                resource_id=row.id,
                details={"outcome": record.specification.get("outcome", "closed")},
            )
            session.commit()
        return True
    except Exception:
        return False
    finally:
        api.close()


def cleanup(session_id):
    with resource_lock(session_id) as acquired:
        return cleanup_owned(session_id) if acquired else False


def enqueue_cleanup(session_id):
    if get_settings().orchestration_inline:
        cleanup(session_id)
    else:
        from app.orchestration.celery_app import cleanup_command_session

        with suppress(Exception):
            cleanup_command_session.delay(str(session_id))  # Durable state is retried by beat.


def close(service, principal, cluster_id, session_id):
    row = find(service, principal, cluster_id, session_id)
    mark_closing(row.id, "user_closed")
    enqueue_cleanup(row.id)
    service.session.refresh(row)
    return public(row)


def attach(service, principal, cluster_id, session_id):
    row = find(service, principal, cluster_id, session_id)
    service.session.refresh(row)
    if (
        row.status != "ready"
        or row.expires_at <= now()
        or datetime.fromisoformat(row.specification["attach_until"]) <= now()
    ):
        raise InvalidStateError("Session already attached or expired")
    cluster = service.cluster(principal, cluster_id)
    api = api_for(service.session, service.secret_store, cluster, allow_command_sessions=True)
    pod = owned(request(api, "GET", ns_path(row) + "/pods/runtime"), row, "pod")
    if pod.get("status", {}).get("phase") != "Running":
        raise InvalidStateError("Session pod is not running")
    service.session.refresh(row, with_for_update=True)
    if (
        row.status != "ready"
        or row.expires_at <= now()
        or datetime.fromisoformat(row.specification["attach_until"]) <= now()
    ):
        raise InvalidStateError("Session was closed or attached while checking its runtime")
    row.status = "active"
    row.heartbeat_at = now()
    audit.record(
        service.session,
        principal=principal,
        action="command_session.attach",
        resource_type="command_session",
        resource_id=row.id,
    )
    target = connection_target(service.session, service.secret_store, service.cluster(principal, cluster_id))
    service.session.commit()
    return namespace(row), target


def heartbeat(principal, session_id):
    permissions(principal)
    with get_session_factory()() as session:
        scope_session(session, principal)
        row = get_row(session, session_id, lock=True)
        if (
            row.created_by != principal.subject
            or row.status != "active"
            or row.expires_at <= now()
            or row.specification["context_project_id"] != (str(principal.project_id) if principal.project_id else None)
        ):
            raise InvalidStateError("Session access, context or lease changed")
        cluster = session.get(Cluster, row.cluster_id)
        if not cluster or cluster.lifecycle_state in {ClusterLifecycleState.DELETING, ClusterLifecycleState.DELETED}:
            raise InvalidStateError("Cluster is being deleted")
        row.heartbeat_at = now()
        session.commit()


def reap():
    # Filter in SQL so live sessions cannot hide later closing/expired sessions.
    stamp = now()
    with get_session_factory()() as session:
        eligible = or_(
            CommandSession.status == "closing",
            CommandSession.expires_at <= stamp,
            and_(
                CommandSession.status == "active",
                or_(
                    CommandSession.heartbeat_at.is_(None),
                    CommandSession.heartbeat_at < stamp - timedelta(seconds=HEARTBEAT_SECONDS),
                ),
            ),
            and_(CommandSession.status == "starting", CommandSession.created_at < stamp - timedelta(seconds=120)),
            and_(
                CommandSession.status == "ready",
                CommandSession.specification["attach_until"].as_string().cast(DateTime(timezone=True)) <= stamp,
            ),
        )
        ids = list(
            session.scalars(
                select(CommandSession.id)
                .where(CommandSession.status != "closed", eligible)
                .order_by(CommandSession.expires_at)
                .limit(20)
            )
        )
    requested = 0
    for session_id in ids:
        if mark_closing(session_id, "lease_or_connection_ended", only_if_due=True):
            cleanup(session_id)
            requested += 1
    return {"cleanup_requested": requested}


def status_hook(session, operation):
    if operation.status in {OperationStatus.FAILED, OperationStatus.CANCELLED}:
        row = session.get(CommandSession, uuid.UUID(operation.params["session_id"]))
        if row and row.status != "closed":
            row.status = "closing"


workflow_registry.register(
    WorkflowDefinition(
        type="cluster.command.session.prepare",
        title="Prepare interactive Command Center",
        description="Create an admin-confirmed disposable runtime with a short-lived bound credential.",
        params_model=SessionParams,
        destructive=True,
        target=lambda p: OperationTarget("cluster", str(p.cluster_id), p.cluster_name, exclusive=False),
        plan=lambda p: [StepDefinition("prepare", "Prepare disposable session runtime", prepare)],
        on_status=status_hook,
    )
)
