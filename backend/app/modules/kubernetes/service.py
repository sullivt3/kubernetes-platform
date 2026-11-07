"""Cluster connectivity: import, connection settings, live reads and health observation."""

from __future__ import annotations

import logging
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.errors import (
    ConfirmationRequiredError,
    ConflictError,
    InvalidStateError,
    NotFoundError,
    PlatformError,
    ValidationFailedError,
)
from app.core.principal import Principal
from app.core.secrets import SecretStore
from app.modules.auditing import service as audit
from app.modules.cluster_lifecycle.distributions.types import DistributionType
from app.modules.cluster_lifecycle.models import Cluster, ClusterLifecycleState, ClusterOrigin
from app.modules.credentials.models import Credential, CredentialKind
from app.modules.credentials.service import CredentialService
from app.modules.kubernetes import client as kclient
from app.modules.kubernetes import kubeconfig, reads, views
from app.orchestration.models import Operation
from app.orchestration.service import OperationService

logger = logging.getLogger(__name__)

NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
HOSTNAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.\-:]{0,251}[A-Za-z0-9])?$")
# States in which live observation (probe, connect) may change the lifecycle state.
OBSERVABLE = {
    ClusterLifecycleState.PENDING,
    ClusterLifecycleState.ACTIVE,
    ClusterLifecycleState.DEGRADED,
    ClusterLifecycleState.UNREACHABLE,
}


def _now() -> datetime:
    return datetime.now(UTC)


def validate_connection_settings(server_override: str | None, tls_server_name: str | None) -> None:
    if server_override and not re.match(r"^https://[A-Za-z0-9.\-\[\]:]+(:\d+)?/?$", server_override):
        raise ValidationFailedError("API server override must look like https://host:port")
    if tls_server_name and not HOSTNAME_RE.match(tls_server_name):
        raise ValidationFailedError("TLS server name must be a hostname or IP address")


def api_for(session: Session, secret_store: SecretStore, cluster: Cluster) -> kclient.ClusterAPI:
    """Pooled API client for a cluster. Decrypts the kubeconfig only when (re)building."""
    if cluster.access_credential_id is None:
        raise InvalidStateError(
            "The platform has no credentials for this cluster yet"
            + (" (installation not finished)" if cluster.origin is ClusterOrigin.PROVISIONED else "")
        )
    credential = session.get(Credential, cluster.access_credential_id)
    if credential is None:
        raise InvalidStateError("The cluster's kubeconfig credential was deleted")
    key = (
        cluster.id,
        credential.id,
        credential.updated_at,
        credential.key_id,
        cluster.api_server_override,
        cluster.tls_server_name,
    )

    def target() -> kclient.ConnectionTarget:
        text = CredentialService(session, secret_store).reveal(cluster.organization_id, credential.id).decode()
        return kclient.ConnectionTarget(
            kubeconfig=kubeconfig.resolve(text),
            server_override=cluster.api_server_override,
            tls_server_name=cluster.tls_server_name,
        )

    return kclient.client_for(key, target)


def connection_target(session: Session, secret_store: SecretStore, cluster: Cluster) -> kclient.ConnectionTarget:
    """Decrypted connection material for non-pooled transports (exec WebSockets)."""
    if cluster.access_credential_id is None:
        raise InvalidStateError("The platform has no credentials for this cluster yet")
    text = (
        CredentialService(session, secret_store).reveal(cluster.organization_id, cluster.access_credential_id).decode()
    )
    return kclient.ConnectionTarget(
        kubeconfig=kubeconfig.resolve(text),
        server_override=cluster.api_server_override,
        tls_server_name=cluster.tls_server_name,
    )


def record_observation(cluster: Cluster, summary: dict[str, Any] | None, error: PlatformError | None) -> None:
    detail = dict(cluster.status_detail or {})
    if summary is not None:
        trimmed = {k: v for k, v in summary.items() if k != "readyz_checks"}
        detail["health"] = trimmed
        detail["observed_at"] = _now().isoformat()
        detail.pop("health_error", None)
        cluster.last_contact_at = _now()
        if summary.get("version"):
            cluster.kubernetes_version = summary["version"]
        if cluster.lifecycle_state in OBSERVABLE:
            cluster.lifecycle_state = ClusterLifecycleState(views.health_state(summary))
    elif error is not None:
        detail["health_error"] = {
            "code": error.code,
            "message": error.message,
            "observed_at": _now().isoformat(),
        }
        if cluster.lifecycle_state in OBSERVABLE:
            cluster.lifecycle_state = ClusterLifecycleState.UNREACHABLE
    cluster.status_detail = detail


class KubernetesService:
    def __init__(self, session: Session, secret_store: SecretStore, operations: OperationService) -> None:
        self.session = session
        self.secret_store = secret_store
        self.operations = operations

    # ------------------------------------------------------------------ helpers

    def cluster(self, principal: Principal, cluster_id: uuid.UUID) -> Cluster:
        cluster = self.session.get(Cluster, cluster_id, populate_existing=True)
        if cluster is None or cluster.organization_id != principal.organization_id:
            raise NotFoundError("Cluster not found")
        return cluster

    def api(self, principal: Principal, cluster_id: uuid.UUID) -> tuple[Cluster, kclient.ClusterAPI]:
        cluster = self.cluster(principal, cluster_id)
        if cluster.lifecycle_state in (ClusterLifecycleState.DELETING, ClusterLifecycleState.DELETED):
            raise InvalidStateError("The cluster is being deleted")
        return cluster, api_for(self.session, self.secret_store, cluster)

    # ------------------------------------------------------------------ live reads

    def health(self, principal: Principal, cluster_id: uuid.UUID) -> dict[str, Any]:
        cluster, api = self.api(principal, cluster_id)
        try:
            summary = reads.summary(api)
        except PlatformError as exc:
            record_observation(cluster, None, exc)
            self.session.commit()
            raise
        record_observation(cluster, summary, None)
        self.session.commit()
        return summary

    def nodes(self, principal: Principal, cluster_id: uuid.UUID) -> list[dict[str, Any]]:
        _, api = self.api(principal, cluster_id)
        with ThreadPoolExecutor(max_workers=3) as pool:
            f_nodes, f_pods, f_metrics = (
                pool.submit(reads.nodes, api),
                pool.submit(reads.pods, api),
                pool.submit(reads.node_metrics, api),
            )
            node_list, (pod_list, _), metrics = f_nodes.result(), f_pods.result(), f_metrics.result()
        usage, by_node = views.node_usage(metrics), views.pods_by_node(pod_list)
        return [
            views.node_view(n, by_node.get(n["metadata"]["name"], []), usage.get(n["metadata"]["name"]))
            for n in node_list
        ]

    def node(self, principal: Principal, cluster_id: uuid.UUID, name: str) -> dict[str, Any]:
        _, api = self.api(principal, cluster_id)
        node = kclient.get_json(api, f"/api/v1/nodes/{name}")
        pod_list, _ = reads.pods(api, node=name)
        usage = views.node_usage(reads.node_metrics(api)).get(name)
        view = views.node_view(node, pod_list, usage)
        view["pod_list"] = [views.pod_view(p) for p in pod_list]
        view["events"] = reads.events(
            api, field_selector=f"involvedObject.kind=Node,involvedObject.name={name}", limit=50
        )
        return view

    def namespaces(self, principal: Principal, cluster_id: uuid.UUID) -> list[dict[str, Any]]:
        _, api = self.api(principal, cluster_id)
        ns_list = reads.namespaces(api)
        pod_list, _ = reads.pods(api)
        by_ns: dict[str, list[dict[str, Any]]] = {}
        for pod in pod_list:
            by_ns.setdefault(pod["metadata"]["namespace"], []).append(pod)
        return [views.namespace_view(ns, by_ns.get(ns["metadata"]["name"], [])) for ns in ns_list]

    def pods(
        self,
        principal: Principal,
        cluster_id: uuid.UUID,
        *,
        namespace: str | None,
        node: str | None,
        problems_only: bool,
    ) -> dict[str, Any]:
        _, api = self.api(principal, cluster_id)
        pod_list, truncated = reads.pods(api, namespace, node=node)
        items = [views.pod_view(p) for p in pod_list]
        if problems_only:
            items = [p for p in items if p["problem"] or p["phase"] in ("Pending", "Failed")]
        items.sort(key=lambda p: (p["namespace"] or "", p["name"] or ""))
        return {"items": items, "truncated": truncated}

    def events(
        self,
        principal: Principal,
        cluster_id: uuid.UUID,
        *,
        namespace: str | None,
        warnings_only: bool,
        limit: int,
    ) -> list[dict[str, Any]]:
        _, api = self.api(principal, cluster_id)
        return reads.events(api, namespace, "type=Warning" if warnings_only else None, limit=limit)

    def topology(self, principal: Principal, cluster_id: uuid.UUID) -> dict[str, Any]:
        nodes = self.nodes(principal, cluster_id)
        _, api = self.api(principal, cluster_id)
        pod_list, truncated = reads.pods(api)
        by_node = views.pods_by_node(pod_list)
        return {
            "nodes": [
                {
                    **{k: n[k] for k in ("name", "roles", "ready", "status", "internal_ip", "cpu", "memory", "pods")},
                    "pod_list": [views.pod_view(p) for p in by_node.get(n["name"], [])],
                }
                for n in nodes
            ],
            "unscheduled": [views.pod_view(p) for p in by_node.get("", [])],
            "truncated": truncated,
        }

    # ------------------------------------------------------------------ import & connection

    @staticmethod
    def inspect_kubeconfig(text: str) -> dict[str, Any]:
        doc = kubeconfig.parse(text)
        return {
            "current_context": kubeconfig.current_context(doc),
            "contexts": [c.__dict__ for c in kubeconfig.list_contexts(doc)],
        }

    def import_cluster(
        self,
        principal: Principal,
        *,
        name: str,
        kubeconfig_text: str,
        context: str | None,
        server_override: str | None,
        tls_server_name: str | None,
        description: str | None,
    ) -> tuple[Cluster, Operation]:
        if not NAME_RE.match(name):
            raise ValidationFailedError("Cluster names must be lowercase letters, digits and '-'")
        validate_connection_settings(server_override, tls_server_name)
        if self.session.scalar(
            select(Cluster.id).where(Cluster.organization_id == principal.organization_id, Cluster.name == name)
        ):
            raise ConflictError(f"A cluster named '{name}' already exists")
        minified, context_name = kubeconfig.minify(kubeconfig.parse(kubeconfig_text), context)
        resolved = kubeconfig.resolve(kubeconfig.dump(minified))

        credential = CredentialService(self.session, self.secret_store).create(
            principal,
            name=f"cluster-{name}-kubeconfig",
            kind=CredentialKind.KUBECONFIG,
            secret_value=kubeconfig.dump(minified),
            description=f"Kubeconfig for imported cluster {name} (context {context_name})",
            public_metadata={"cluster": name, "context": context_name},
        )
        cluster = Cluster(
            organization_id=principal.organization_id,
            name=name,
            description=description,
            distribution=DistributionType.OTHER,
            origin=ClusterOrigin.IMPORTED,
            lifecycle_state=ClusterLifecycleState.PENDING,
            api_endpoint=resolved.server,
            access_credential_id=credential.id,
            kubeconfig_context=context_name,
            api_server_override=server_override or None,
            tls_server_name=tls_server_name or None,
            spec={},
        )
        self.session.add(cluster)
        self.session.flush()
        audit.record(
            self.session,
            principal=principal,
            action="cluster.import",
            resource_type="cluster",
            resource_id=cluster.id,
            resource_name=name,
            details={"context": context_name, "server": resolved.server, "server_override": server_override},
        )
        self.session.commit()
        return cluster, self.connect(principal, cluster.id)

    def connect(self, principal: Principal, cluster_id: uuid.UUID) -> Operation:
        from app.modules.kubernetes.workflows import CLUSTER_CONNECT

        cluster = self.cluster(principal, cluster_id)
        return self.operations.submit(
            principal, CLUSTER_CONNECT, {"cluster_id": str(cluster.id), "cluster_name": cluster.name}
        )

    def update_connection(
        self,
        principal: Principal,
        cluster_id: uuid.UUID,
        *,
        server_override: str | None,
        tls_server_name: str | None,
    ) -> Operation:
        validate_connection_settings(server_override, tls_server_name)
        cluster = self.cluster(principal, cluster_id)
        before = {"server_override": cluster.api_server_override, "tls_server_name": cluster.tls_server_name}
        cluster.api_server_override = server_override or None
        cluster.tls_server_name = tls_server_name or None
        audit.record(
            self.session,
            principal=principal,
            action="cluster.update_connection",
            resource_type="cluster",
            resource_id=cluster.id,
            resource_name=cluster.name,
            details={
                "before": before,
                "after": {"server_override": server_override, "tls_server_name": tls_server_name},
            },
        )
        self.session.commit()
        kclient.forget(cluster.id)
        return self.connect(principal, cluster.id)

    def install_metrics_server(
        self, principal: Principal, cluster_id: uuid.UUID, *, kubelet_insecure_tls: bool
    ) -> Operation:
        from app.modules.kubernetes.workflows import METRICS_SERVER

        cluster = self.cluster(principal, cluster_id)
        if cluster.access_credential_id is None:
            raise InvalidStateError("The cluster is not connected yet")
        return self.operations.submit(
            principal,
            METRICS_SERVER,
            {
                "cluster_id": str(cluster.id),
                "cluster_name": cluster.name,
                "kubelet_insecure_tls": kubelet_insecure_tls,
            },
        )

    def unregister(self, principal: Principal, cluster_id: uuid.UUID, *, confirm: str | None) -> None:
        cluster = self.cluster(principal, cluster_id)
        if cluster.origin is not ClusterOrigin.IMPORTED:
            raise ValidationFailedError(
                "Clusters created by the platform are removed with Delete, which also resets their nodes"
            )
        if confirm != cluster.name:
            raise ConfirmationRequiredError(
                "The platform will forget this cluster and its stored kubeconfig. The cluster itself is not changed. "
                "Repeat the name to confirm.",
                details={"expected_confirmation": cluster.name},
            )
        if self.operations.list_operations(
            principal, active_only=True, target_type="cluster", target_id=str(cluster.id), limit=1
        )[1]:
            raise ConflictError("An operation is in progress for this cluster")
        credential_id = cluster.access_credential_id
        audit.record(
            self.session,
            principal=principal,
            action="cluster.unregister",
            resource_type="cluster",
            resource_id=cluster.id,
            resource_name=cluster.name,
        )
        cluster.access_credential_id = None
        self.session.flush()
        self.session.delete(cluster)
        if credential_id:
            credential = self.session.get(Credential, credential_id)
            if credential is not None:
                self.session.delete(credential)
        self.session.commit()
        kclient.forget(cluster_id)


# ---------------------------------------------------------------------- periodic probe


def probe_clusters(session_factory: sessionmaker[Session], secret_store: SecretStore) -> dict[str, int]:
    """Observe every connected cluster (called periodically by the scheduler)."""
    with session_factory() as session:
        ids = list(
            session.scalars(
                select(Cluster.id).where(
                    Cluster.access_credential_id.is_not(None), Cluster.lifecycle_state.in_(OBSERVABLE)
                )
            )
        )

    def probe(cluster_id: uuid.UUID) -> str:
        with session_factory() as session:
            cluster = session.get(Cluster, cluster_id)
            if cluster is None or cluster.lifecycle_state not in OBSERVABLE:
                return "skipped"
            try:
                summary = reads.summary(api_for(session, secret_store, cluster))
                record_observation(cluster, summary, None)
                result = "ok"
            except PlatformError as exc:
                record_observation(cluster, None, exc)
                result = "unreachable"
            except Exception as exc:
                logger.exception("Probe of cluster %s failed", cluster_id)
                record_observation(cluster, None, PlatformError(str(exc)))
                result = "unreachable"
            session.commit()
            return result

    counts: dict[str, int] = {}
    if ids:
        with ThreadPoolExecutor(max_workers=min(8, len(ids))) as pool:
            for outcome in pool.map(probe, ids):
                counts[outcome] = counts.get(outcome, 0) + 1
    return counts
