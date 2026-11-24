"""Bounded mutations through the existing workflow and Helm review machinery."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from app.core.errors import ConflictError, InvalidStateError
from app.modules.cluster_lifecycle.models import Cluster
from app.modules.custom_workflows import service as actions
from app.modules.helm import service as helm
from app.modules.helm.models import HelmPlan
from app.modules.kubernetes import reviewed_operations as tasks
from app.modules.kubernetes.custom_resource_models import CustomResourceReview
from app.modules.kubernetes.custom_resources import request
from app.modules.kubernetes.service import connection_target

CATEGORY = "command_center"


def is_mutation(plan):
    return plan["verb"] in {"scale", "rollout", "rollback"}


def digest(spec):
    return hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()


def release_path(plan, revision):
    return f"/api/v1/namespaces/{plan['namespace']}/secrets/sh.helm.release.v1.{plan['name']}.v{revision}"


def simulate(runner, plan):
    # Helm's server simulation is not an admission dry run of every affected object.
    # No hooks, post-renderers or arbitrary user flags are permitted.
    runner.run(
        [
            "rollback",
            plan["name"],
            str(plan["revision"]),
            "--namespace",
            plan["namespace"],
            "--dry-run=server",
            "--no-hooks",
            "--timeout",
            "30s",
        ],
        timeout=30,
    )


def review(service, principal, cluster, plan):
    _, api = service.api(principal, cluster.id)
    if plan["tool"] == "kubectl":
        step = actions.Step(
            action="resource.scale" if plan["verb"] == "scale" else "resource.restart",
            kind="Deployment" if plan["resource"] == "deployments" else "StatefulSet",
            name=plan["name"],
            namespace=plan["namespace"],
            replicas=plan.get("replicas"),
        )
        live = request(api, "GET", actions.path_for(step))
        item, _ = actions.prepare_mutation(api, step, live, "command:" + uuid.uuid4().hex)
        changes: dict[str, Any] = {
            "kind": step.kind,
            "namespace": step.namespace,
            "target": step.name,
            "uid": item["uid"],
            "dry_run": "Kubernetes server dry run passed",
        }
        if plan["verb"] == "scale":
            before = live.get("spec", {}).get("replicas", 1)
            changes.update(before_replicas=before if type(before) is int else None, replicas=step.replicas)
        else:
            changes["restart_at"] = item["desired"]["spec"]["template"]["metadata"]["annotations"][
                "kubectl.kubernetes.io/restartedAt"
            ]
        changes["notice"] = (
            "Only this action and its recovery marker are changed. API acceptance is not rollout readiness. "
            "Controllers may change the resource later."
        )
        return {**plan, "mutation": item}, changes

    reviewed = helm.preview(
        service,
        principal,
        cluster.id,
        helm.PlanInput(
            action="rollback",
            release=plan["name"],
            namespace=plan["namespace"],
            revision=plan["revision"],
            hooks=False,
            timeout_seconds=120,
        ),
    )
    hid = uuid.UUID(str(reviewed["id"]))
    spec = reviewed["specification"]
    snapshots = {
        str(rev): actions.mutation_fingerprint(request(api, "GET", release_path(plan, rev)))
        for rev in {spec["expected_revision"], plan["revision"]}
    }
    with helm.Runner(connection_target(service.session, service.secret_store, cluster)) as runner:
        simulate(runner, plan)
        inventories = {}
        for rev in {spec["expected_revision"], plan["revision"]}:
            rendered = runner.run(
                ["get", "manifest", plan["name"], "--namespace", plan["namespace"], "--revision", str(rev)], timeout=30
            )
            inventories[rev] = helm.inventory(rendered, plan["namespace"])
    for revision, expected in snapshots.items():
        if actions.mutation_fingerprint(request(api, "GET", release_path(plan, revision))) != expected:
            raise ConflictError("Helm revision changed during review")
    current = inventories[spec["expected_revision"]]
    desired = inventories[plan["revision"]]
    keys = {(r["kind"], r["namespace"], r["name"]) for r in desired}
    removed = [r for r in current if (r["kind"], r["namespace"], r["name"]) not in keys]
    return {**plan, "mutation": {"helm_plan_id": str(hid), "spec_hash": digest(spec), "snapshots": snapshots}}, {
        "kind": "Helm release",
        "target": plan["name"],
        "namespace": plan["namespace"],
        "expected_revision": spec["expected_revision"],
        "revision": plan["revision"],
        "resources": desired,
        "removed_resources": removed,
        "dry_run": "Helm server simulation passed; hooks disabled",
        "notice": (
            "Rollback can change or remove chart resources. Simulation does not prove all admission checks or deletion "
            "effects. Pause external Helm writers; they are not fenced. Helm waits up to 120 seconds; "
            "accepted changes remain on failure or cancellation."
        ),
    }


def execute(ctx, api, plan):
    def reauthorize():
        tasks.access(ctx, CATEGORY)

    if plan["tool"] == "kubectl":
        live = actions.apply_mutation(api, plan["mutation"], reauthorize)
        return {
            "action": plan["verb"],
            "name": plan["name"],
            "namespace": plan["namespace"],
            "uid": live["metadata"]["uid"],
            "status": "Accepted",
            "rollout_verified": False,
        }

    item = plan["mutation"]
    hid = uuid.UUID(item["helm_plan_id"])
    with ctx.db() as session:
        row = session.scalar(select(HelmPlan).where(HelmPlan.id == hid).with_for_update())
        review = session.get(CustomResourceReview, ctx.params.review_id)
        if (
            not row
            or not review
            or row.cluster_id != ctx.params.cluster_id
            or row.created_by != review.created_by
            or row.operation_id not in {None, ctx.operation_id}
            or digest(row.specification) != item["spec_hash"]
        ):
            raise InvalidStateError("Bound Helm plan changed or is no longer available")
        row.approved_at = datetime.now(UTC)  # Command approval was checked by tasks.access.
        row.operation_id = ctx.operation_id

    def guard():
        reauthorize()
        for revision, expected in item["snapshots"].items():
            if actions.mutation_fingerprint(request(api, "GET", release_path(plan, revision))) != expected:
                raise ConflictError("Helm revision identity/content changed after review")
        with ctx.db() as session:
            row = session.get(HelmPlan, hid)
            if not row or digest(row.specification) != item["spec_hash"]:
                raise InvalidStateError("Bound Helm plan changed")

    # Simulate again under the same review before the existing executor's final guard.
    # Recovery of an already-deployed owned revision is handled by execute_plan.
    reauthorize()
    with ctx.db() as session:
        cluster = session.get(Cluster, ctx.params.cluster_id)
        if not cluster:
            raise InvalidStateError("Cluster no longer available")
        target = connection_target(session, ctx.secret_store, cluster)
    with helm.Runner(target, ctx) as runner:
        current = runner.release(plan["namespace"], plan["name"])
        if current and int(current["revision"]) == max(int(v) for v in item["snapshots"]):
            guard()
            simulate(runner, plan)
    return helm.execute_plan(
        ctx,
        helm.HelmParams(
            cluster_id=ctx.params.cluster_id,
            cluster_name=ctx.params.cluster_name,
            plan_id=hid,
            action="rollback",
            release=plan["name"],
        ),
        guard,
    )
