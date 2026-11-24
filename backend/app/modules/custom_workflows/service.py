"""Encrypted reusable sequences of bounded Kubernetes actions and checks."""

from __future__ import annotations

import copy
import hashlib
import json
import time
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator
from sqlalchemy import select

from app.core.errors import (
    ConfirmationRequiredError,
    ConflictError,
    InvalidStateError,
    NotFoundError,
    ValidationFailedError,
)
from app.core.secrets import SealedSecret
from app.modules.auditing import service as audit
from app.modules.custom_workflows.models import CustomWorkflow
from app.modules.gitops.pruning import ready
from app.modules.helm.catalog import values as validate_mapping
from app.modules.kubernetes import reviewed_operations as tasks
from app.modules.kubernetes.custom_resources import DNS, NAME, fingerprint, request
from app.modules.organizations.policy import require
from app.orchestration.workflow import OperationTarget, StepDefinition, WorkflowDefinition, workflow_registry

MARKER = "kplatform.io/workflow-step"
KINDS = {
    "Deployment": ("apps/v1", "deployments"),
    "StatefulSet": ("apps/v1", "statefulsets"),
    "DaemonSet": ("apps/v1", "daemonsets"),
    "Pod": ("v1", "pods"),
    "Job": ("batch/v1", "jobs"),
}


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["cluster.check", "resource.scale", "resource.restart", "resource.wait"]
    kind: Literal["Deployment", "StatefulSet", "DaemonSet", "Pod", "Job"] = "Deployment"
    namespace: str = Field(default="default", pattern=DNS, max_length=63)
    name: str = Field(default="", pattern=r"^$|" + NAME, max_length=253)
    replicas: int | None = Field(default=None, ge=0, le=1000, strict=True)
    timeout_seconds: int = Field(default=120, ge=1, le=600, strict=True)

    @model_validator(mode="after")
    def valid_action(self):
        if self.action != "cluster.check" and not self.name:
            raise ValueError("Resource name required")
        if self.action == "resource.scale" and (
            self.kind not in {"Deployment", "StatefulSet"} or self.replicas is None
        ):
            raise ValueError("Scaling requires a Deployment/StatefulSet and replica count")
        if self.action == "resource.restart" and self.kind != "Deployment":
            raise ValueError("Restart supports Deployments")
        return self


class Document(BaseModel):
    model_config = ConfigDict(extra="forbid")
    steps: list[Step] = Field(min_length=1, max_length=20)


class WorkflowInput(BaseModel):
    name: str = Field(pattern=DNS, max_length=63)
    document: SecretStr
    generation: int | None = Field(default=None, ge=1)


def parse_document(text):
    validate_mapping(text)
    try:
        return Document.model_validate(yaml.safe_load(text))
    except ValidationError:
        raise ValidationFailedError(
            "Workflow requires 1-20 supported steps with valid resource names and parameters. Raw input withheld."
        ) from None


def public(row):
    return {"id": row.id, "name": row.name, "generation": row.generation, "step_count": row.step_count}


def find(service, principal, cluster_id, workflow_id):
    service.cluster(principal, cluster_id)
    row = service.session.scalar(
        select(CustomWorkflow).where(CustomWorkflow.id == workflow_id, CustomWorkflow.cluster_id == cluster_id)
    )
    if not row:
        raise NotFoundError("Custom workflow not found")
    return row


def save(service, principal, cluster_id, body, workflow_id=None):
    require(principal, "organization.manage")
    cluster = service.cluster(principal, cluster_id)
    doc = parse_document(body.document.get_secret_value())
    row = find(service, principal, cluster_id, workflow_id) if workflow_id else None
    if row:
        service.session.refresh(row, with_for_update=True)
        if row.generation != body.generation:
            raise ConflictError("Workflow changed; reveal and reload before saving")
    sealed = service.secret_store.seal(doc.model_dump_json().encode())
    if not row:
        row = CustomWorkflow(
            cluster_id=cluster.id, organization_id=cluster.organization_id, project_id=cluster.project_id, generation=1
        )
        service.session.add(row)
    else:
        row.generation += 1
    row.name = body.name
    row.step_count = len(doc.steps)
    row.key_id = sealed.key_id
    row.ciphertext = sealed.ciphertext
    service.session.flush()
    audit.record(
        service.session,
        principal=principal,
        action="custom_workflow.save",
        resource_type="custom_workflow",
        resource_id=row.id,
    )
    service.session.commit()
    return public(row)


def reveal(service, principal, cluster_id, workflow_id):
    require(principal, "secrets.reveal")
    row = find(service, principal, cluster_id, workflow_id)
    doc = json.loads(service.secret_store.unseal(SealedSecret(row.key_id, row.ciphertext)))
    audit.record(
        service.session,
        principal=principal,
        action="custom_workflow.reveal",
        resource_type="custom_workflow",
        resource_id=row.id,
    )
    service.session.commit()
    return {**public(row), "yaml": yaml.safe_dump(doc, sort_keys=False)}


def forget(service, principal, cluster_id, workflow_id, confirm):
    require(principal, "organization.manage")
    row = find(service, principal, cluster_id, workflow_id)
    if confirm != row.name:
        raise ConfirmationRequiredError("Repeat the workflow name")
    audit.record(
        service.session,
        principal=principal,
        action="custom_workflow.forget",
        resource_type="custom_workflow",
        resource_id=row.id,
    )
    service.session.delete(row)
    service.session.commit()
    return {"removed": True}


def path_for(step):
    version, plural = KINDS[step.kind]
    return (
        ("/api/v1" if version == "v1" else "/apis/" + version)
        + "/namespaces/"
        + step.namespace
        + "/"
        + plural
        + "/"
        + step.name
    )


def check_cluster(api):
    status, _ = api.request("GET", "/readyz")
    if status != 200:
        raise InvalidStateError("Cluster API is not ready")
    nodes = request(api, "GET", "/api/v1/nodes").get("items", [])
    if not nodes or any(
        not any(
            c.get("type") == "Ready" and c.get("status") == "True"
            for c in (n.get("status") or {}).get("conditions", [])
        )
        for n in nodes
    ):
        raise InvalidStateError("One or more cluster nodes are not Ready")
    return {"ready_nodes": len(nodes)}


def mutation_fingerprint(obj):
    """Diff normalization alone omits UID and last-applied; writes must bind both."""
    metadata = obj.get("metadata") or {}
    material = {
        "content": fingerprint(obj),
        "uid": metadata.get("uid"),
        "last_applied": (metadata.get("annotations") or {}).get("kubectl.kubernetes.io/last-applied-configuration"),
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


def prepare_mutation(api, step, live, marker):
    """Review one bounded change, preserving all unrelated fields and metadata."""
    if step.action not in {"resource.scale", "resource.restart"} or live["metadata"].get("deletionTimestamp"):
        raise InvalidStateError("Invalid or deleting mutation target")
    desired = copy.deepcopy(live)
    desired.pop("status", None)
    desired["metadata"].pop("managedFields", None)
    if step.action == "resource.scale":
        desired.setdefault("spec", {})["replicas"] = step.replicas
    else:
        desired["spec"]["template"].setdefault("metadata", {}).setdefault("annotations", {})[
            "kubectl.kubernetes.io/restartedAt"
        ] = datetime.now(UTC).isoformat()
    desired["metadata"].setdefault("annotations", {})[MARKER] = marker
    path = path_for(step)
    planned = request(api, "PUT", path, body=desired, params={"dryRun": "All", "fieldValidation": "Strict"})
    if mutation_fingerprint(planned) != mutation_fingerprint(desired):
        raise InvalidStateError("Admission changes fields beyond the bounded action")
    return {
        "path": path,
        "uid": live["metadata"]["uid"],
        "before": mutation_fingerprint(live),
        "desired": desired,
        "after": mutation_fingerprint(planned),
        "hash_version": 2,
        "marker": marker,
    }, planned


def apply_mutation(api, item, reauthorize):
    """UID/content guards plus resourceVersion CAS; replay only an exact owned result."""
    reauthorize()
    if item.get("hash_version") != 2:
        raise InvalidStateError("Mutation review predates stronger content guards; review again")
    live = request(api, "GET", item["path"])
    if live["metadata"]["uid"] != item["uid"] or live["metadata"].get("deletionTimestamp"):
        raise InvalidStateError("Mutation target identity changed")
    already = (live["metadata"].get("annotations") or {}).get(MARKER) == item["marker"]
    if mutation_fingerprint(live) != (item["after"] if already else item["before"]):
        raise ConflictError("Mutation target changed after review")
    if not already:
        desired = copy.deepcopy(item["desired"])
        desired["metadata"]["resourceVersion"] = live["metadata"]["resourceVersion"]
        planned = request(api, "PUT", item["path"], body=desired, params={"dryRun": "All", "fieldValidation": "Strict"})
        if mutation_fingerprint(planned) != item["after"]:
            raise ConflictError("Admission changed after review; review again")
        reauthorize()
        live = request(api, "PUT", item["path"], body=desired, params={"fieldValidation": "Strict"})
    if mutation_fingerprint(live) != item["after"]:
        raise ConflictError("Admission changed the accepted action; completed writes are retained")
    return live


def preview(service, principal, cluster_id, workflow_id):
    require(principal, "clusters.write")
    cluster, api = service.api(principal, cluster_id)
    row = find(service, principal, cluster_id, workflow_id)
    generation = row.generation
    doc = Document.model_validate_json(service.secret_store.unseal(SealedSecret(row.key_id, row.ciphertext)))
    snapshots: dict[str, Any] = {}
    steps = []
    nonce = uuid.uuid4().hex
    for index, step in enumerate(doc.steps):
        item: dict[str, Any] = {"step": step.model_dump()}
        if step.action == "cluster.check":
            check_cluster(api)
        else:
            path = path_for(step)
            live = snapshots.get(path) or request(api, "GET", path)
            if live["metadata"].get("deletionTimestamp"):
                raise InvalidStateError("A workflow target is being deleted")
            item.update(path=path, uid=live["metadata"]["uid"], before=fingerprint(live))
            if step.action in {"resource.scale", "resource.restart"}:
                prepared, planned = prepare_mutation(api, step, live, nonce + ":" + str(index))
                item.update(prepared)
                snapshots[path] = planned
        steps.append(item)
    service.session.refresh(row)
    if row.generation != generation:
        raise ConflictError("Workflow changed during review")
    summary = {
        "workflow_id": str(row.id),
        "generation": generation,
        "steps": [{"position": i + 1, **v["step"]} for i, v in enumerate(steps)],
    }
    return tasks.persist(
        service,
        principal,
        cluster,
        "custom_workflow",
        row.name,
        {"workflow_id": str(row.id), "generation": generation, "steps": steps},
        summary,
    )


def guard(ctx, payload):
    with ctx.db() as session:
        row = session.get(CustomWorkflow, uuid.UUID(payload["workflow_id"]))
        if not row or row.cluster_id != ctx.params.cluster_id or row.generation != payload["generation"]:
            raise InvalidStateError("Workflow was removed or changed after review")


def execute_step(ctx):
    api, payload, spec = tasks.access(ctx, "custom_workflow")
    guard(ctx, payload)
    if len(payload["steps"]) != ctx.params.step_count:
        raise InvalidStateError("Workflow plan changed")
    index = int(ctx.step_key.split("-")[1])
    item = payload["steps"][index]
    step = Step.model_validate(item["step"])
    if step.action == "cluster.check":
        result = check_cluster(api)
    else:
        live = request(api, "GET", item["path"])
        if live["metadata"]["uid"] != item["uid"] or live["metadata"].get("deletionTimestamp"):
            raise InvalidStateError("Workflow target identity changed")
        if step.action == "resource.wait":
            started = spec.get("progress", {}).get("wait-" + ctx.step_key) or datetime.now(UTC).isoformat()
            tasks.progress(ctx, **{"wait-" + ctx.step_key: started})
            while not ready(live):
                if (datetime.now(UTC) - datetime.fromisoformat(started)).total_seconds() >= step.timeout_seconds:
                    raise InvalidStateError("Resource readiness timed out")
                time.sleep(1)
                api, payload, _ = tasks.access(ctx, "custom_workflow")
                guard(ctx, payload)
                live = request(api, "GET", item["path"])
                if live["metadata"]["uid"] != item["uid"] or live["metadata"].get("deletionTimestamp"):
                    raise InvalidStateError("Wait target changed")
            result = {"ready": True, "uid": item["uid"]}
        else:

            def reauthorize():
                tasks.access(ctx, "custom_workflow")
                guard(ctx, payload)

            live = apply_mutation(api, item, reauthorize)
            result = {"uid": live["metadata"]["uid"], "accepted": True}
    tasks.progress(ctx, **{ctx.step_key: "complete"})
    if index == ctx.params.step_count - 1:
        ctx.set_result({"completed_steps": ctx.params.step_count, "status": "Completed"})
    return result


workflow_registry.register(
    WorkflowDefinition(
        type="cluster.custom_workflow",
        title="Custom workflow",
        description="Run a reviewed sequence of bounded Kubernetes actions and checks.",
        params_model=tasks.ReviewedParams,
        destructive=True,
        target=lambda p: OperationTarget("cluster", str(p.cluster_id), p.cluster_name),
        plan=lambda p: [
            StepDefinition("step-" + str(i), "Custom workflow step " + str(i + 1), tasks.safe_step(execute_step))
            for i in range(p.step_count)
        ],
    )
)
