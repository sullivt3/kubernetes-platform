"""Audited, bounded DNS queries performed inside the selected cluster."""

from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.core.errors import PlatformError
from app.modules.kubernetes import client as kc
from app.modules.kubernetes.phase6 import workflow_api
from app.modules.kubernetes.workflows import ConnectParams, _call
from app.orchestration.context import StepContext
from app.orchestration.workflow import (
    OperationTarget,
    StepDefinition,
    StepFailedError,
    WorkflowDefinition,
    workflow_registry,
)

DNS_LOOKUP = "cluster.dns.lookup"
DNS_IMAGE = "registry.k8s.io/e2e-test-images/dnsutils:1.3"
OP_LABEL = "kubernetes-platform.io/operation"


class DnsLookup(BaseModel):
    namespace: str = Field(default="default", min_length=1, max_length=63, pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
    hostname: str = Field(
        default="kubernetes.default.svc",
        min_length=1,
        max_length=253,
        pattern=r"^[A-Za-z0-9_]([A-Za-z0-9_.-]*[A-Za-z0-9_.])?$",
    )
    record_type: Literal["A", "AAAA", "CNAME", "SRV", "TXT", "MX"] = "A"


class DnsParams(ConnectParams, DnsLookup):
    pass


def lookup_job(name: str, operation_id: str, p: DnsLookup) -> dict[str, Any]:
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name, "namespace": p.namespace, "labels": {OP_LABEL: operation_id}},
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": 60,
            "ttlSecondsAfterFinished": 300,
            "template": {
                "metadata": {"labels": {OP_LABEL: operation_id}},
                "spec": {
                    "restartPolicy": "Never",
                    "automountServiceAccountToken": False,
                    "dnsPolicy": "ClusterFirst",
                    "nodeSelector": {"kubernetes.io/os": "linux"},
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 1000,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [
                        {
                            "name": "lookup",
                            "image": DNS_IMAGE,
                            "command": ["nslookup", "-timeout=5", "-retry=1", f"-type={p.record_type}", p.hostname],
                            "resources": {
                                "requests": {"cpu": "10m", "memory": "16Mi"},
                                "limits": {"cpu": "100m", "memory": "64Mi"},
                            },
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]},
                            },
                        }
                    ],
                },
            },
        },
    }


def run_lookup(ctx: StepContext, api: kc.ClusterAPI, p: DnsParams) -> dict[str, Any]:
    name = f"kp-dns-{ctx.operation_id.hex}"
    collection = f"/apis/batch/v1/namespaces/{p.namespace}/jobs"
    path = f"{collection}/{name}"
    try:
        job = kc.get_json(api, path)
    except kc.NotFoundInClusterError:
        job = kc.send_json(api, "POST", collection, lookup_job(name, str(ctx.operation_id), p))
    if (job.get("metadata", {}).get("labels") or {}).get(OP_LABEL) != str(ctx.operation_id):
        raise StepFailedError("A different resource already uses the diagnostic job name", code="dns_job_conflict")
    uid = job["metadata"]["uid"]
    deadline = time.monotonic() + 90
    ctx.info(f"Querying {p.hostname} ({p.record_type}) from namespace {p.namespace}")
    try:
        while True:
            ctx.check_cancelled()
            pods, _ = kc.list_items(
                api, f"/api/v1/namespaces/{p.namespace}/pods", {"labelSelector": f"{OP_LABEL}={ctx.operation_id}"}
            )
            owned = [
                pod
                for pod in pods
                if any(ref.get("uid") == uid for ref in pod.get("metadata", {}).get("ownerReferences") or [])
            ]
            for pod in owned:
                terminated = next(
                    (
                        s.get("state", {}).get("terminated")
                        for s in pod.get("status", {}).get("containerStatuses") or []
                        if s.get("name") == "lookup"
                    ),
                    None,
                )
                if terminated is not None:
                    output = kc.get_text(
                        api,
                        f"/api/v1/namespaces/{p.namespace}/pods/{pod['metadata']['name']}/log",
                        {"container": "lookup", "limitBytes": 32768},
                        timeout=10,
                    )
                    result = {
                        "hostname": p.hostname,
                        "record_type": p.record_type,
                        "namespace": p.namespace,
                        "resolved": terminated.get("exitCode") == 0,
                        "exit_code": terminated.get("exitCode"),
                        "output": output,
                    }
                    ctx.info(output or "The DNS utility returned no output")
                    ctx.set_result(result)
                    return result
            job = kc.get_json(api, path)
            failed = next(
                (
                    c
                    for c in job.get("status", {}).get("conditions") or []
                    if c.get("type") == "Failed" and c.get("status") == "True"
                ),
                None,
            )
            if failed or time.monotonic() >= deadline:
                problems = [
                    c.get("message")
                    for pod in owned
                    for c in pod.get("status", {}).get("conditions") or []
                    if c.get("status") == "False" and c.get("message")
                ]
                raise StepFailedError(
                    "DNS diagnostic could not run. "
                    + (
                        (failed or {}).get("message")
                        or "; ".join(problems)
                        or "Timed out waiting for the diagnostic pod; "
                        "check scheduling, image pulls and admission policies."
                    ),
                    code="dns_diagnostic_failed",
                )
            time.sleep(2)
    finally:
        try:
            kc.send_json(api, "DELETE", path, {"propagationPolicy": "Background", "preconditions": {"uid": uid}})
        except kc.NotFoundInClusterError:
            pass
        except PlatformError as exc:
            ctx.warning(f"Diagnostic cleanup failed: {exc.message}. The Job TTL will remove it after completion.")


def step_lookup(ctx: StepContext) -> dict[str, Any]:
    p: DnsParams = ctx.params  # type: ignore[assignment]
    return _call(lambda: run_lookup(ctx, workflow_api(ctx, p.cluster_id), p))


workflow_registry.register(
    WorkflowDefinition(
        type=DNS_LOOKUP,
        title="Run DNS lookup",
        description="Run a temporary DNS diagnostic Job and collect its output.",
        params_model=DnsParams,
        plan=lambda _p: [StepDefinition("lookup", "Query DNS inside the cluster", step_lookup)],
        target=lambda p: OperationTarget(type="cluster", id=str(p.cluster_id), name=p.cluster_name),
        title_for=lambda p: f"DNS lookup {p.hostname} ({p.cluster_name})",
    )
)
