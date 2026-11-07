"""Cluster connectivity workflows: connect/verify and the metrics-server add-on."""

from __future__ import annotations

import time
import urllib.request
import uuid
from typing import Any

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.errors import PlatformError
from app.modules.cluster_lifecycle.distributions.types import DistributionType
from app.modules.cluster_lifecycle.models import Cluster, ClusterOrigin
from app.modules.kubernetes import apply as kapply
from app.modules.kubernetes import reads, views
from app.modules.kubernetes.client import ClusterAuthError, ClusterUnreachableError
from app.modules.kubernetes.service import api_for, record_observation
from app.orchestration.context import StepContext
from app.orchestration.models import Operation, OperationStatus
from app.orchestration.workflow import (
    OperationTarget,
    RetryableStepError,
    StepDefinition,
    StepFailedError,
    WorkflowDefinition,
    workflow_registry,
)

CLUSTER_CONNECT = "cluster.connect"
METRICS_SERVER = "cluster.addon.metrics_server"
METRICS_SERVER_VERSION = "v0.9.1"
METRICS_SERVER_URL = "https://github.com/kubernetes-sigs/metrics-server/releases/download/{version}/components.yaml"


class ConnectParams(BaseModel):
    cluster_id: uuid.UUID
    cluster_name: str


class MetricsServerParams(ConnectParams):
    kubelet_insecure_tls: bool = False


def _api(ctx: StepContext, cluster_id: uuid.UUID) -> Any:
    with ctx.db() as session:
        cluster = session.get(Cluster, cluster_id)
        if cluster is None:
            raise StepFailedError("Cluster no longer exists", code="cluster_missing")
        try:
            return api_for(session, ctx.secret_store, cluster)
        except PlatformError as exc:
            raise StepFailedError(exc.message, code=exc.code) from exc


def _call(fn: Any, *args: Any) -> Any:
    try:
        return fn(*args)
    except ClusterUnreachableError as exc:
        raise RetryableStepError(exc.message) from exc
    except ClusterAuthError as exc:
        raise StepFailedError(exc.message, code=exc.code) from exc
    except PlatformError as exc:
        raise StepFailedError(exc.message, code=exc.code) from exc


# --------------------------------------------------------------------------- connect


def step_connect(ctx: StepContext) -> dict[str, Any]:
    p: ConnectParams = ctx.params  # type: ignore[assignment]
    api = _api(ctx, p.cluster_id)
    version = _call(reads.version, api)
    ready, checks = _call(reads.readyz, api)
    ctx.info(f"Connected: Kubernetes {version.get('gitVersion')} ({version.get('platform')})")
    failing = [c["name"] for c in checks if not c["ok"]]
    if ready:
        ctx.info(f"API server ready ({len(checks)} readiness checks passed)")
    else:
        ctx.warning(f"API server reports not ready: {', '.join(failing) or 'unknown'}")
    return {"version": version.get("gitVersion"), "api_ready": ready}


def step_inventory(ctx: StepContext) -> dict[str, Any]:
    p: ConnectParams = ctx.params  # type: ignore[assignment]
    api = _api(ctx, p.cluster_id)
    summary = _call(reads.summary, api)
    node_list = _call(reads.nodes, api)
    distribution = views.detect_distribution(
        {"gitVersion": summary["version"]}, node_list, _call(reads.has_kubeadm_config, api)
    )
    with ctx.db() as session:
        cluster = session.get(Cluster, p.cluster_id)
        if cluster is None:
            raise StepFailedError("Cluster no longer exists", code="cluster_missing")
        record_observation(cluster, summary, None)
        if cluster.origin is ClusterOrigin.IMPORTED:
            cluster.distribution = distribution
        state = cluster.lifecycle_state.value
    ctx.info(
        f"{summary['nodes']['ready']}/{summary['nodes']['total']} nodes ready, "
        f"{summary['pods']['running']}/{summary['pods']['total']} pods running"
    )
    if distribution is not DistributionType.OTHER:
        ctx.info(f"Detected distribution: {distribution.value}")
    if not summary["metrics_available"]:
        ctx.info("Resource metrics (metrics-server) are not available; utilization shows requested resources")
    for alert in summary["alerts"]:
        ctx.warning(alert["message"])
    ctx.set_result({"state": state, "version": summary["version"], "nodes": summary["nodes"], "pods": summary["pods"]})
    return {"state": state}


def on_connect_status(session: Session, op: Operation) -> None:
    if op.status is not OperationStatus.FAILED:
        return
    cluster = session.get(Cluster, uuid.UUID(op.params["cluster_id"]))
    if cluster is None:
        return
    error = op.error or {}
    record_observation(cluster, None, PlatformError(error.get("message", "Connection failed")))
    detail = dict(cluster.status_detail or {})
    detail["health_error"] = {
        **detail.get("health_error", {}),
        "code": error.get("code"),
        "operation_id": str(op.id),
    }
    cluster.status_detail = detail


workflow_registry.register(
    WorkflowDefinition(
        type=CLUSTER_CONNECT,
        title="Connect to cluster",
        description="Verify API access, read the inventory and record cluster health.",
        params_model=ConnectParams,
        plan=lambda _p: [
            StepDefinition(
                "connect",
                "Connect to the Kubernetes API",
                step_connect,
                max_attempts=3,
                retry_backoff_seconds=5,
            ),
            StepDefinition(
                "inventory",
                "Read nodes, pods and health",
                step_inventory,
                max_attempts=2,
                retry_backoff_seconds=5,
            ),
        ],
        target=lambda p: OperationTarget(type="cluster", id=str(p.cluster_id), name=p.cluster_name),
        title_for=lambda p: f"Connect to cluster {p.cluster_name}",
        on_status=on_connect_status,
    )
)


# --------------------------------------------------------------------------- metrics-server


def step_apply_metrics_server(ctx: StepContext) -> dict[str, Any]:
    p: MetricsServerParams = ctx.params  # type: ignore[assignment]
    url = METRICS_SERVER_URL.format(version=METRICS_SERVER_VERSION)
    ctx.info(f"Downloading metrics-server {METRICS_SERVER_VERSION} manifest")
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            text = response.read().decode()
    except OSError as exc:
        raise RetryableStepError(f"Could not download {url}: {exc}") from exc
    objects = kapply.parse_manifest(text)
    if p.kubelet_insecure_tls:
        for obj in objects:
            if obj.get("kind") == "Deployment":
                for container in obj["spec"]["template"]["spec"]["containers"]:
                    args = container.setdefault("args", [])
                    if "--kubelet-insecure-tls" not in args:
                        args.append("--kubelet-insecure-tls")
        ctx.warning("metrics-server will not verify kubelet serving certificates (--kubelet-insecure-tls)")
    api = _api(ctx, p.cluster_id)
    applied = _call(lambda: kapply.apply_objects(api, objects, on_applied=lambda o: ctx.info(f"applied {o}")))
    return {"applied": len(applied), "version": METRICS_SERVER_VERSION}


def step_wait_metrics(ctx: StepContext) -> dict[str, Any]:
    p: MetricsServerParams = ctx.params  # type: ignore[assignment]
    api = _api(ctx, p.cluster_id)
    deadline = time.monotonic() + 300
    while True:
        ctx.check_cancelled()
        metrics = _call(reads.node_metrics, api)
        if metrics and metrics.get("items"):
            usage = views.node_usage(metrics)
            for name, u in usage.items():
                ctx.info(f"{name}: {u['cpu_m']}m CPU, {u['memory_bytes'] // (1024 * 1024)} MiB memory in use")
            ctx.set_result({"metrics_server": METRICS_SERVER_VERSION, "nodes_reporting": len(usage)})
            return {"nodes_reporting": len(usage)}
        if time.monotonic() > deadline:
            raise StepFailedError(
                "The metrics API did not become available within 5 minutes. On kubeadm clusters kubelet serving "
                "certificates are self-signed; retry with 'skip kubelet certificate verification' if the "
                "metrics-server logs show x509 errors.",
                code="metrics_unavailable",
            )
        time.sleep(5)


workflow_registry.register(
    WorkflowDefinition(
        type=METRICS_SERVER,
        title="Install metrics-server",
        description="Install metrics-server so live CPU and memory usage is available.",
        params_model=MetricsServerParams,
        plan=lambda _p: [
            StepDefinition(
                "apply",
                "Apply metrics-server manifest",
                step_apply_metrics_server,
                max_attempts=3,
                retry_backoff_seconds=10,
            ),
            StepDefinition("wait", "Wait for the metrics API", step_wait_metrics),
        ],
        target=lambda p: OperationTarget(type="cluster", id=str(p.cluster_id), name=p.cluster_name),
        title_for=lambda p: f"Install metrics-server on {p.cluster_name}",
    )
)
