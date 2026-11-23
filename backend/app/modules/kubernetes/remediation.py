"""Opt-in detection, durable approval and PDB-aware replacement of failing stateless pods."""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select, text

from app.core.errors import InvalidStateError
from app.core.principal import Principal
from app.modules.auditing import service as audit
from app.modules.cluster_lifecycle.models import Cluster, ClusterLifecycleState
from app.modules.kubernetes import client as kc
from app.modules.kubernetes.phase6 import workflow_api
from app.modules.kubernetes.remediation_models import RemediationRequest
from app.modules.kubernetes.service import api_for
from app.modules.kubernetes.service_proxy import DNSLabel
from app.modules.kubernetes.workflows import ConnectParams
from app.orchestration.context import StepContext
from app.orchestration.workflow import (
    OperationTarget,
    StepDefinition,
    StepFailedError,
    WorkflowDefinition,
    workflow_registry,
)

logger = logging.getLogger(__name__)
WORKFLOW = "cluster.remediation.replace_pod"
INACTIVE = {ClusterLifecycleState.DELETING, ClusterLifecycleState.DELETED}


class RemediationPolicy(BaseModel):
    enabled: bool = False
    namespaces: list[DNSLabel] = Field(default_factory=list, max_length=20)
    grace_seconds: int = Field(default=300, ge=60, le=3600)
    cooldown_minutes: int = Field(default=30, ge=5, le=1440)

    @model_validator(mode="after")
    def validate_scope(self) -> RemediationPolicy:
        self.namespaces = sorted(set(self.namespaces))
        if self.enabled and not self.namespaces:
            raise ValueError("Select at least one namespace")
        if any(n.startswith("kube-") or n == "kplatform-monitoring" for n in self.namespaces):
            raise ValueError("System and platform monitoring namespaces cannot be remediated")
        return self


class ReplaceParams(ConnectParams):
    request_id: uuid.UUID


def failing_reason(pod: dict) -> str | None:
    for container in pod.get("status", {}).get("containerStatuses", []):
        if container.get("restartCount", 0) < 3 or container.get("ready"):
            continue
        state = container.get("state") or {}
        if state.get("waiting", {}).get("reason") == "CrashLoopBackOff":
            return "CrashLoopBackOff"
        if state.get("terminated", {}).get("exitCode", 0) != 0:
            return "Container failed"
    return None


def digest(obj: dict) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def optional(api: kc.ClusterAPI, path: str) -> dict | None:
    try:
        return kc.get_json(api, path)
    except kc.NotFoundInClusterError:
        return None


def snapshot(api: kc.ClusterAPI, pod: dict) -> dict:
    """Refuse static, stateful, critical and host-coupled pods; pin the full owner chain."""
    meta, spec = pod["metadata"], pod.get("spec", {})
    ns = meta["namespace"]
    if ns.startswith("kube-") or ns == "kplatform-monitoring" or meta.get("deletionTimestamp"):
        raise InvalidStateError("The pod is protected or already terminating")
    if (
        any(spec.get(key) for key in ("hostNetwork", "hostPID", "hostIPC"))
        or any(
            c.get("securityContext", {}).get("privileged")
            for c in spec.get("containers", []) + spec.get("initContainers", [])
        )
        or spec.get("priority", 0) >= 1_000_000_000
        or "kubernetes.io/config.mirror" in meta.get("annotations", {})
        or any(
            not set(v).issubset({"name", "projected", "secret", "configMap", "downwardAPI"})
            for v in spec.get("volumes", [])
        )
    ):
        raise InvalidStateError("The pod uses host access, local/persistent data or critical priority")
    owner: dict = next((o for o in meta.get("ownerReferences", []) if o.get("controller")), {})
    if owner.get("kind") != "ReplicaSet":
        raise InvalidStateError("Only pods owned by a Deployment ReplicaSet are supported")
    rs = kc.get_json(api, f"/apis/apps/v1/namespaces/{ns}/replicasets/{owner['name']}")
    parent: dict = next((o for o in rs["metadata"].get("ownerReferences", []) if o.get("controller")), {})
    if rs["metadata"]["uid"] != owner["uid"] or parent.get("kind") != "Deployment":
        raise InvalidStateError("The pod owner chain has changed")
    deployment = kc.get_json(api, f"/apis/apps/v1/namespaces/{ns}/deployments/{parent['name']}")
    if (
        deployment["metadata"]["uid"] != parent["uid"]
        or deployment["metadata"].get("deletionTimestamp")
        or deployment["spec"].get("paused")
        or deployment["spec"].get("replicas", 1) < 1
        or rs["spec"].get("replicas", 0) < 1
    ):
        raise InvalidStateError("The Deployment is paused, scaled down, deleting or replaced")
    return {
        "replicaset_name": owner["name"],
        "replicaset_uid": owner["uid"],
        "deployment_name": parent["name"],
        "deployment_uid": parent["uid"],
        "replicaset_template": digest(rs["spec"]["template"]),
        "deployment_template": digest(deployment["spec"]["template"]),
    }


def cooldown(
    session: Any, cluster_id: uuid.UUID, owner_uid: str, minutes: int, *, exclude: uuid.UUID | None = None
) -> bool:
    rows = session.scalars(
        select(RemediationRequest).where(
            RemediationRequest.cluster_id == cluster_id,
            RemediationRequest.approved_at > datetime.now(UTC) - timedelta(minutes=minutes),
        )
    )
    return any(r.id != exclude and r.snapshot["deployment_uid"] == owner_uid for r in rows)


def scan_cluster(session_factory: Any, store: Any, cluster_id: uuid.UUID) -> dict:
    """A scan proposes work only; it can never evict or mutate Kubernetes resources."""
    key = int.from_bytes(hashlib.sha256(b"remediation" + cluster_id.bytes).digest()[:8], "big", signed=True)
    with session_factory() as lock_session:
        if not lock_session.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": key}):
            return {"skipped": True}
        with session_factory() as session:
            cluster = session.get(Cluster, cluster_id)
            if cluster is None or cluster.lifecycle_state in INACTIVE:
                return {"skipped": True}
            policy = RemediationPolicy.model_validate(cluster.remediation_policy or {})
            if not policy.enabled:
                return {"skipped": True}
            config = cluster.remediation_policy
            api = api_for(session, store, cluster)
        pods, truncated = kc.list_items(api, "/api/v1/pods", max_items=1000)
        if truncated:
            raise InvalidStateError("Pod inventory was truncated; remediation scan did not change proposals")
        candidates = []
        for pod in pods:
            if pod["metadata"].get("namespace") not in policy.namespaces or not failing_reason(pod):
                continue
            try:
                candidates.append((pod, snapshot(api, pod)))
            except (InvalidStateError, kc.NotFoundInClusterError):
                continue
            if len(candidates) >= 50:
                raise InvalidStateError("Too many candidates; narrow remediation namespace scope")
        now = datetime.now(UTC)
        with session_factory() as session:
            cluster = session.scalar(select(Cluster).where(Cluster.id == cluster_id).with_for_update())
            if cluster is None or cluster.remediation_policy != config or cluster.lifecycle_state in INACTIVE:
                return {"skipped": True}
            rows = {
                r.pod_uid: r
                for r in session.scalars(select(RemediationRequest).where(RemediationRequest.cluster_id == cluster_id))
            }
            seen = set()
            for pod, owner in candidates:
                meta = pod["metadata"]
                uid = meta["uid"]
                seen.add(uid)
                row = rows.get(uid)
                if row is None:
                    if cooldown(session, cluster_id, owner["deployment_uid"], policy.cooldown_minutes):
                        continue
                    row = RemediationRequest(
                        organization_id=cluster.organization_id,
                        project_id=cluster.project_id,
                        cluster_id=cluster_id,
                        namespace=meta["namespace"],
                        pod_name=meta["name"],
                        pod_uid=uid,
                        snapshot=owner,
                        reason=failing_reason(pod),
                        status="observing",
                        first_seen_at=now,
                        last_seen_at=now,
                    )
                    session.add(row)
                else:
                    if row.status in {"resolved", "revoked", "expired"} and row.approved_at is None:
                        row.status = "observing"
                        row.first_seen_at = now
                        row.expires_at = None
                        row.snapshot = owner
                    # A missed scan or template change restarts the stability window.
                    if row.status == "observing" and (
                        row.snapshot != owner or now - row.last_seen_at > timedelta(seconds=180)
                    ):
                        row.first_seen_at = now
                        row.snapshot = owner
                    row.last_seen_at = now
                if row.status == "observing" and now - row.first_seen_at >= timedelta(seconds=policy.grace_seconds):
                    row.status = "pending"
                    row.expires_at = now + timedelta(minutes=15)
                    audit.record(
                        session,
                        principal=replace(
                            Principal.system(cluster.organization_id, "remediation"), project_id=cluster.project_id
                        ),
                        action="remediation.proposed",
                        resource_type="cluster",
                        resource_id=cluster.id,
                        resource_name=cluster.name,
                        details={"pod": row.pod_name, "namespace": row.namespace},
                    )
            for uid, row in rows.items():
                if row.status not in {"observing", "pending"}:
                    continue
                if uid not in seen:
                    row.status = "resolved"
                elif row.expires_at and row.expires_at < now:
                    row.status = "expired"
            cluster.status_detail = {
                **(cluster.status_detail or {}),
                "remediation": {"checked_at": now.isoformat(), "status": "ok"},
            }
            session.commit()
            return {"candidates": len(candidates)}


def scan_all(session_factory: Any, store: Any) -> dict:
    with session_factory() as session:
        ids = list(session.scalars(select(Cluster.id).where(Cluster.remediation_policy.is_not(None))))
    report = {"checked": 0, "failed": 0}
    for cluster_id in ids:
        try:
            scan_cluster(session_factory, store, cluster_id)
            report["checked"] += 1
        except Exception:
            logger.warning("Remediation observation failed for cluster %s", cluster_id)
            report["failed"] += 1
            with session_factory() as session:
                cluster = session.get(Cluster, cluster_id)
                if cluster is not None:
                    cluster.status_detail = {
                        **(cluster.status_detail or {}),
                        "remediation": {
                            "checked_at": datetime.now(UTC).isoformat(),
                            "status": "unavailable",
                        },
                    }
                    session.commit()
    return report


def replace_pod(ctx: StepContext) -> dict:
    p: ReplaceParams = ctx.params  # type: ignore[assignment]
    api = workflow_api(ctx, p.cluster_id)
    ctx.check_cancelled()
    with ctx.db() as session:
        cluster = session.scalar(select(Cluster).where(Cluster.id == p.cluster_id).with_for_update())
        row = session.scalar(select(RemediationRequest).where(RemediationRequest.id == p.request_id).with_for_update())
        if cluster is None or row is None or row.cluster_id != p.cluster_id:
            raise StepFailedError("Remediation approval not found")
        policy = RemediationPolicy.model_validate(cluster.remediation_policy or {})
        if (
            not policy.enabled
            or row.namespace not in policy.namespaces
            or row.status != "approved"
            or not row.approved_by
            or row.expires_at is None
            or row.expires_at < datetime.now(UTC)
            or row.operation_id not in {None, ctx.operation_id}
        ):
            raise StepFailedError("Remediation approval expired, was revoked, or the policy was disabled")
        if cooldown(session, p.cluster_id, row.snapshot["deployment_uid"], policy.cooldown_minutes, exclude=row.id):
            raise StepFailedError("Another approved attempt is within this Deployment's cooldown")
        row.operation_id = ctx.operation_id
        pod_path = f"/api/v1/namespaces/{row.namespace}/pods/{row.pod_name}"
        pod = optional(api, pod_path)
        output = {"namespace": row.namespace, "pod_name": row.pod_name, "pod_uid": row.pod_uid, **row.snapshot}
        if pod is None or pod["metadata"]["uid"] != row.pod_uid:
            ctx.info("Original pod is already gone; no replacement pod was evicted")
            return {**output, "action": "already_gone"}
        if snapshot(api, pod) != row.snapshot:
            raise StepFailedError("Workload changed after review; this approval cannot be used")
        if not failing_reason(pod):
            ctx.info("Pod has recovered; no eviction needed")
            return {**output, "action": "skipped_recovered"}
        ctx.check_cancelled()
        try:
            kc.send_json(
                api,
                "POST",
                pod_path + "/eviction",
                {
                    "apiVersion": "policy/v1",
                    "kind": "Eviction",
                    "metadata": {"namespace": row.namespace, "name": row.pod_name},
                    "deleteOptions": {
                        "preconditions": {"uid": row.pod_uid, "resourceVersion": pod["metadata"]["resourceVersion"]}
                    },
                },
            )
        except kc.KubernetesAPIError as exc:
            if exc.http_status == 429:
                raise StepFailedError(
                    "Eviction was blocked by a disruption budget or API throttling; no force deletion was attempted"
                ) from None
            raise
        ctx.info(f"Eviction accepted for {row.namespace}/{row.pod_name}; waiting for its Deployment")
        return {**output, "action": "evicted"}


def verify_replacement(ctx: StepContext) -> dict:
    p: ReplaceParams = ctx.params  # type: ignore[assignment]
    result = ctx.outputs["replace"]
    if result["action"] == "skipped_recovered":
        ctx.set_result(result)
        return result
    api = workflow_api(ctx, p.cluster_id)
    ns = result["namespace"]
    for _ in range(90):
        ctx.check_cancelled()
        deployment = kc.get_json(api, f"/apis/apps/v1/namespaces/{ns}/deployments/{result['deployment_name']}")
        if (
            deployment["metadata"]["uid"] != result["deployment_uid"]
            or digest(deployment["spec"]["template"]) != result["deployment_template"]
        ):
            raise StepFailedError("Deployment changed while replacement was being verified")
        pods, truncated = kc.list_items(api, f"/api/v1/namespaces/{ns}/pods", max_items=1000)
        ready = [
            pod
            for pod in pods
            if pod["metadata"]["uid"] != result["pod_uid"]
            and any(
                o.get("controller") and o.get("uid") == result["replicaset_uid"]
                for o in pod["metadata"].get("ownerReferences", [])
            )
            and not pod["metadata"].get("deletionTimestamp")
            and any(c["type"] == "Ready" and c["status"] == "True" for c in pod.get("status", {}).get("conditions", []))
        ]
        status = deployment.get("status", {})
        if (
            not truncated
            and ready
            and status.get("observedGeneration", 0) >= deployment["metadata"]["generation"]
            and status.get("availableReplicas", 0) >= deployment["spec"].get("replicas", 1)
        ):
            final = {**result, "replacement_ready": True, "replacement_pod": ready[0]["metadata"]["name"]}
            ctx.set_result(final)
            return final
        time.sleep(2)
    raise StepFailedError(
        "Replacement did not become Ready within three minutes. Inspect logs and events; no further pods were evicted."
    )


workflow_registry.register(
    WorkflowDefinition(
        type=WORKFLOW,
        title="Remediate repeatedly failing pod",
        description="Evict one approved stateless pod and verify its replacement.",
        params_model=ReplaceParams,
        destructive=True,
        plan=lambda _: [
            StepDefinition("replace", "Revalidate approval and evict one pod", replace_pod),
            StepDefinition("verify", "Verify replacement readiness", verify_replacement),
        ],
        target=lambda p: OperationTarget("cluster", str(p.cluster_id), p.cluster_name),
    )
)
