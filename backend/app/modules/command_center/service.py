"""Reviewed, read-only command subset. Kubernetes commands use the REST transport."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any, NoReturn

from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import select

from app.core.errors import InvalidStateError, NotFoundError, ValidationFailedError
from app.core.secrets import SealedSecret
from app.modules.auditing import service as audit
from app.modules.cluster_lifecycle.models import Cluster
from app.modules.helm.runtime import Runner, public_history, public_release
from app.modules.kubernetes import reviewed_operations as tasks
from app.modules.kubernetes.client import KubeClient
from app.modules.kubernetes.custom_resource_models import CustomResourceReview
from app.modules.kubernetes.service import connection_target
from app.modules.organizations.policy import require
from app.orchestration.workflow import OperationTarget, StepDefinition, WorkflowDefinition, workflow_registry

CATEGORY = "command_center"
RAW_LIMIT = 2 * 1024 * 1024
OUTPUT_LIMIT = 64 * 1024
# Explicit built-ins only. No discovery, subresources, Secrets, configuration or raw YAML.
RESOURCES = {
    "nodes": ("/api/v1", False),
    "namespaces": ("/api/v1", False),
    "pods": ("/api/v1", True),
    "services": ("/api/v1", True),
    "deployments": ("/apis/apps/v1", True),
    "replicasets": ("/apis/apps/v1", True),
    "statefulsets": ("/apis/apps/v1", True),
    "daemonsets": ("/apis/apps/v1", True),
    "jobs": ("/apis/batch/v1", True),
    "cronjobs": ("/apis/batch/v1", True),
    "persistentvolumeclaims": ("/api/v1", True),
    "persistentvolumes": ("/api/v1", False),
    "storageclasses": ("/apis/storage.k8s.io/v1", False),
}
ALIASES = {
    "no": "nodes",
    "ns": "namespaces",
    "po": "pods",
    "svc": "services",
    "deploy": "deployments",
    "rs": "replicasets",
    "sts": "statefulsets",
    "ds": "daemonsets",
    "pvc": "persistentvolumeclaims",
    "pv": "persistentvolumes",
    "sc": "storageclasses",
}
DNS = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?\Z")
NAME = re.compile(r"[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?\Z")


class CommandInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: SecretStr = Field(json_schema_extra={"maxLength": 512})


def invalid() -> NoReturn:
    raise ValidationFailedError(
        "Supported: kubectl get RESOURCE [NAME] [-n NAMESPACE | -A], "
        "helm list [-n NAMESPACE | -A], helm history RELEASE [-n NAMESPACE]. "
        "Only documented resource summaries are supported; other flags and shell syntax are refused."
    )


def parse(text: str) -> dict[str, Any]:
    if not text or len(text) > 512 or not re.fullmatch(r"[a-zA-Z0-9_.= /-]+", text):
        invalid()
    tokens = text.split()
    if len(tokens) < 2:
        invalid()
    tool, verb, *rest = tokens
    if (tool, verb) not in {("kubectl", "get"), ("helm", "list"), ("helm", "history")}:
        invalid()
    positional = []
    namespace = None
    all_namespaces = False
    while rest:
        token = rest.pop(0)
        if token in {"-n", "--namespace"} or token.startswith("--namespace="):
            if namespace is not None or all_namespaces:
                invalid()
            if token.startswith("--namespace="):
                namespace = token.split("=", 1)[1]
            elif rest:
                namespace = rest.pop(0)
            else:
                invalid()
            if not DNS.fullmatch(namespace):
                invalid()
        elif token in {"-A", "--all-namespaces"}:
            if namespace is not None or all_namespaces:
                invalid()
            all_namespaces = True
        elif token.startswith("-"):
            invalid()
        else:
            positional.append(token)
    resource = name = None
    if tool == "kubectl":
        if not 1 <= len(positional) <= 2:
            invalid()
        resource = ALIASES.get(positional[0], positional[0])
        if resource not in RESOURCES:
            invalid()
        if len(positional) == 2:
            name = positional[1]
        if not RESOURCES[resource][1] and (namespace is not None or all_namespaces):
            invalid()
    elif verb == "list":
        if positional:
            invalid()
    else:
        if len(positional) != 1 or all_namespaces:
            invalid()
        name = positional[0]
        if not DNS.fullmatch(name) or len(name) > 53:
            invalid()
    if name and (not NAME.fullmatch(name) or ".." in name or all_namespaces):
        invalid()
    namespace = None if all_namespaces or (resource and not RESOURCES[resource][1]) else namespace or "default"
    canonical = f"{tool} {verb}"
    if resource:
        canonical += " " + resource
    if name:
        canonical += " " + name
    if all_namespaces:
        canonical += " -A"
    elif namespace:
        canonical += " -n " + namespace
    return {
        "command": canonical,
        "tool": tool,
        "verb": verb,
        "resource": resource,
        "name": name,
        "namespace": namespace,
        "all_namespaces": all_namespaces,
    }


def review(service, principal, cluster_id, body):
    require(principal, "terminal.exec")
    require(principal, "clusters.write")
    cluster = service.cluster(principal, cluster_id)
    plan = parse(body.command.get_secret_value())
    return tasks.persist(
        service, principal, cluster, CATEGORY, cluster.name, plan, {"command": plan["command"], "read_only": True}
    )


def find(service, principal, cluster_id, review_id):
    require(principal, "terminal.exec")
    service.cluster(principal, cluster_id)
    row = service.session.scalar(
        select(CustomResourceReview).where(
            CustomResourceReview.id == review_id,
            CustomResourceReview.cluster_id == cluster_id,
            CustomResourceReview.created_by == principal.subject,
            CustomResourceReview.specification["category"].as_string() == CATEGORY,
        )
    )
    if not row:
        raise NotFoundError("Command review not found")
    return row


def output(service, principal, cluster_id, review_id):
    row = find(service, principal, cluster_id, review_id)
    payload = json.loads(service.secret_store.unseal(SealedSecret(row.key_id, row.ciphertext)))
    audit.record(
        service.session, principal=principal, action="command_center.output", resource_type="review", resource_id=row.id
    )
    service.session.commit()
    return {"id": row.id, "operation_id": row.specification.get("operation_id"), "result": payload.get("result")}


def history(service, principal, cluster_id):
    require(principal, "terminal.exec")
    service.cluster(principal, cluster_id)
    rows = service.session.scalars(
        select(CustomResourceReview)
        .where(
            CustomResourceReview.cluster_id == cluster_id,
            CustomResourceReview.created_by == principal.subject,
            CustomResourceReview.specification["category"].as_string() == CATEGORY,
        )
        .order_by(CustomResourceReview.created_at.desc())
        .limit(20)
    )
    return [
        {
            "id": r.id,
            "created_at": r.created_at,
            "command": r.specification["summary"]["command"],
            "operation_id": r.specification.get("operation_id"),
        }
        for r in rows
    ]


def safe_value(value):
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        return re.sub(r"[\x00-\x1f\x7f-\x9f]", "", str(value))[:253]
    return ""


def summary(obj):
    metadata = obj.get("metadata") or {}
    status = obj.get("status") or {}
    row = {
        "name": safe_value(metadata.get("name")),
        "namespace": safe_value(metadata.get("namespace")),
        "kind": safe_value(obj.get("kind")),
        "uid": safe_value(metadata.get("uid")),
    }
    phase = status.get("phase")
    if phase in {
        "Pending",
        "Running",
        "Succeeded",
        "Failed",
        "Unknown",
        "Active",
        "Terminating",
        "Available",
        "Bound",
        "Released",
        "Lost",
    }:
        row["phase"] = phase
    ready = next((c.get("status") for c in status.get("conditions", []) if c.get("type") == "Ready"), None)
    if ready in {"True", "False", "Unknown"}:
        row["ready"] = ready
    return row


def kubernetes_read(api, plan):
    prefix, namespaced = RESOURCES[plan["resource"]]
    path = prefix
    if namespaced and plan["namespace"]:
        path += "/namespaces/" + plan["namespace"]
    path += "/" + plan["resource"]
    if plan["name"]:
        path += "/" + plan["name"]
    kwargs: dict[str, Any] = {"params": {} if plan["name"] else {"limit": 100}, "timeout": 10}
    if isinstance(api, KubeClient):
        status, raw = api.request("GET", path, **kwargs, max_response_bytes=RAW_LIMIT)
    else:
        status, raw = api.request("GET", path, **kwargs)
    if status != 200 or len(raw) > RAW_LIMIT:
        raise InvalidStateError("Command request failed or exceeded the response limit; raw response withheld")
    data = json.loads(raw)
    objects = [data] if plan["name"] else data.get("items", [])
    return [summary(obj) for obj in objects[:100]], bool((data.get("metadata") or {}).get("continue")) or len(
        objects
    ) > 100


def execute(ctx):
    api, payload, _ = tasks.access(ctx, CATEGORY)
    if parse(payload["command"]) != {key: value for key, value in payload.items() if key != "result"}:
        raise InvalidStateError("Command plan changed")
    tasks.progress(ctx, started=True)
    if "result" in payload:
        result = payload["result"]
    else:
        if payload["tool"] == "kubectl":
            rows, truncated = kubernetes_read(api, payload)
        else:
            with ctx.db() as session:
                cluster = session.get(Cluster, ctx.params.cluster_id)
                if not cluster:
                    raise NotFoundError("Cluster not found")
                target = connection_target(session, ctx.secret_store, cluster)
            with Runner(target, ctx) as runner:
                args = [payload["verb"], "--output", "json"]
                if payload["verb"] == "history":
                    args += [payload["name"], "--namespace", payload["namespace"], "--max", "50"]
                else:
                    args += ["--all-namespaces"] if payload["all_namespaces"] else ["--namespace", payload["namespace"]]
                    args += ["--max", "101"]
                raw = json.loads(runner.run(args, timeout=20))
                projector = public_history if payload["verb"] == "history" else public_release
                rows = [{k: safe_value(v) for k, v in projector(row).items()} for row in raw[:100]]
                truncated = len(raw) > 100
        # Cap the serialized display, retaining complete JSON rows.
        while len(json.dumps(rows, indent=2, ensure_ascii=True).encode()) > OUTPUT_LIMIT - 1024:
            rows.pop()
            truncated = True
        result = {
            "output": json.dumps(rows, indent=2, ensure_ascii=True),
            "truncated": truncated,
            "observed_at": datetime.now(UTC).isoformat(),
            "row_count": len(rows),
        }
        tasks.access(ctx, CATEGORY)
        with ctx.db() as session:
            row = session.scalar(
                select(CustomResourceReview).where(CustomResourceReview.id == ctx.params.review_id).with_for_update()
            )
            if not row:
                raise NotFoundError("Command review not found")
            sealed = ctx.secret_store.seal(json.dumps({**payload, "result": result}).encode())
            row.key_id, row.ciphertext = sealed.key_id, sealed.ciphertext
    ctx.check_cancelled()
    public = {key: result[key] for key in ("truncated", "observed_at", "row_count")}
    ctx.set_result({"status": "Completed", **public})
    return public


workflow_registry.register(
    WorkflowDefinition(
        type="cluster.command",
        title="Command Center read",
        description="Execute a reviewed read-only command.",
        params_model=tasks.ReviewedParams,
        destructive=True,
        target=lambda p: OperationTarget("cluster", str(p.cluster_id), p.cluster_name),
        plan=lambda p: [StepDefinition("read", "Read reviewed cluster summary", tasks.safe_step(execute))],
    )
)
