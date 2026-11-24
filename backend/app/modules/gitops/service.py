"""Scoped configuration, encrypted commit reviews and recoverable manual sync."""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.core.errors import ConfirmationRequiredError, InvalidStateError, NotFoundError, PlatformError
from app.core.secrets import SealedSecret
from app.modules.auditing import service as audit
from app.modules.cluster_lifecycle.models import Cluster, ClusterLifecycleState
from app.modules.gitops import pruning, resources
from app.modules.gitops.models import GitOpsApplication, GitOpsReview
from app.modules.gitops.policy import Policy, validate
from app.modules.gitops.source import ApplicationInput, Configuration, GitSource, is_ssh
from app.modules.gitops.ssh_source import SSHAuthentication
from app.modules.helm.credentials import Authentication
from app.modules.kubernetes.service import api_for
from app.modules.kubernetes.workflows import ConnectParams
from app.modules.organizations.policy import require
from app.orchestration.models import ACTIVE_STATUSES, Operation
from app.orchestration.workflow import (
    OperationTarget,
    StepDefinition,
    StepFailedError,
    WorkflowDefinition,
    workflow_registry,
)


class SyncParams(ConnectParams):
    application_id: uuid.UUID
    review_id: uuid.UUID


def application(service, principal, cluster_id, application_id):
    cluster = service.cluster(principal, cluster_id)
    if cluster.lifecycle_state in {ClusterLifecycleState.DELETING, ClusterLifecycleState.DELETED}:
        raise InvalidStateError("The cluster is being deleted")
    row = service.session.scalar(
        select(GitOpsApplication).where(
            GitOpsApplication.id == application_id, GitOpsApplication.cluster_id == cluster_id
        )
    )
    if row is None:
        raise NotFoundError("GitOps application not found")
    return cluster, row


def public(row):
    return {
        "id": row.id,
        "name": row.name,
        "configuration": row.configuration,
        "generation": row.generation,
        "authenticated": bool(row.ciphertext),
        "observation": row.observation,
        "inventory": list(row.inventory.values()),
        "automation": {
            **Policy.model_validate(row.automation).model_dump(),
            **{k: v for k, v in row.automation.items() if k in {"approved_by", "approved_at"}},
        },
        "next_reconcile_at": row.next_reconcile_at,
    }


def busy(session, row):
    active = session.scalar(
        select(Operation.id).where(
            Operation.type.in_(["cluster.gitops.sync", "cluster.gitops.reconcile"]),
            Operation.params["application_id"].as_string() == str(row.id),
            Operation.status.in_(ACTIVE_STATUSES),
        )
    )
    if active:
        raise InvalidStateError("Wait for the active application sync before changing its configuration")


def save(service, principal, cluster_id, body: ApplicationInput, application_id=None):
    cluster = service.cluster(principal, cluster_id)
    if cluster.lifecycle_state in {ClusterLifecycleState.DELETING, ClusterLifecycleState.DELETED}:
        raise InvalidStateError("The cluster is being deleted")
    row = application(service, principal, cluster_id, application_id)[1] if application_id else None
    if row:
        service.session.refresh(row, with_for_update=True)
        busy(service.session, row)
        if row.configuration["namespace"] != body.namespace:
            raise InvalidStateError("Create a separate application to change the destination namespace")
    config = Configuration.model_validate(body.model_dump()).model_dump()
    if (body.allow_private_network or body.ca_pem or body.ssh_host_key) and (
        row is None
        or any(
            row.configuration.get(k) != config[k] for k in ("url", "allow_private_network", "ca_pem", "ssh_host_key")
        )
    ):
        require(principal, "organization.manage")
    ssh = is_ssh(body.url)
    if body.authentication and isinstance(body.authentication, SSHAuthentication) != ssh:
        raise InvalidStateError("Use a private key for SSH or password/token authentication for HTTPS")
    if ssh and (
        body.clear_credentials
        or (not body.authentication and not (row and row.ciphertext and is_ssh(row.configuration["url"])))
    ):
        raise InvalidStateError("SSH requires a private key; use HTTPS for anonymous access")
    if body.authentication and body.clear_credentials:
        raise InvalidStateError("Choose credential replacement or removal")
    if (
        row
        and row.ciphertext
        and row.configuration["url"] != body.url
        and not (body.authentication or body.clear_credentials)
    ):
        raise InvalidStateError("Re-enter or explicitly remove credentials when changing repository URLs")
    if row is None:
        row = GitOpsApplication(
            organization_id=cluster.organization_id,
            project_id=cluster.project_id,
            cluster_id=cluster.id,
            name=body.name,
            configuration=config,
            inventory={},
            observation={},
            generation=1,
        )
        service.session.add(row)
    else:
        row.generation += 1
    row.name = body.name
    row.configuration = config
    row.observation = {}
    row.automation = {**Policy.model_validate(row.automation or {}).model_dump(), "enabled": False}
    row.next_reconcile_at = None
    if body.authentication:
        auth = body.authentication
        if isinstance(auth, SSHAuthentication):
            payload = {
                "mode": "ssh",
                "private_key": auth.private_key.get_secret_value(),
                "passphrase": auth.passphrase.get_secret_value() if auth.passphrase else None,
            }
        else:
            payload = {"mode": auth.mode, "username": auth.username, "password": auth.password.get_secret_value()}
        sealed = service.secret_store.seal(json.dumps(payload).encode())
        row.key_id, row.ciphertext = sealed.key_id, sealed.ciphertext
    elif body.clear_credentials:
        row.key_id = None
        row.ciphertext = None
    service.session.flush()
    audit.record(
        service.session,
        principal=principal,
        action="gitops.configure",
        resource_type="gitops_application",
        resource_id=row.id,
        details={"cluster_id": str(cluster.id)},
    )
    service.session.commit()
    return public(row)


def auth_header(row, store):
    if not row.ciphertext:
        return None
    payload = store.unseal(SealedSecret(row.key_id, row.ciphertext))
    try:
        if is_ssh(row.configuration["url"]):
            return SSHAuthentication.model_validate_json(payload)
        auth = Authentication.model_validate_json(payload)
    except ValueError:
        raise InvalidStateError("Saved repository credentials are invalid; replace authentication") from None
    if auth.mode == "bearer":
        return "Bearer " + auth.password.get_secret_value()
    return "Basic " + base64.b64encode((auth.username + ":" + auth.password.get_secret_value()).encode()).decode()


def plan_resources(api, row, objects, *, prune=False):
    reviewed, summary = resources.review_objects(api, objects, row.id, row.configuration["namespace"])
    paths = {item["path"] for item in reviewed}
    retained = [item for path, item in row.inventory.items() if path not in paths]
    deletions = []
    prune_summary = []
    if prune:
        deletions, prune_summary, retained = pruning.candidates(
            api, row.inventory, paths, row.id, row.configuration["namespace"]
        )
    return {"apply": reviewed, "prune": deletions}, {
        "resources": summary,
        "prune": prune_summary,
        "retained": retained,
        "prune_enabled": prune,
    }


def persist_review(session, store, row, subject, commit, payload, summary, *, automatic=False, operation_id=None):
    sealed = store.seal(json.dumps(payload).encode())
    review = GitOpsReview(
        organization_id=row.organization_id,
        project_id=row.project_id,
        application_id=row.id,
        generation=row.generation,
        created_by=subject,
        commit_sha=commit,
        key_id=sealed.key_id,
        ciphertext=sealed.ciphertext,
        summary={**summary, "automatic": automatic},
        expires_at=datetime.now(UTC) + timedelta(minutes=15),
        approved_at=datetime.now(UTC) if automatic else None,
        operation_id=operation_id,
    )
    session.add(review)
    blocked = any(item["action"] in {"conflict", "blocked"} for item in summary["resources"] + summary["prune"])
    changed = any(item["action"] != "unchanged" for item in summary["resources"]) or any(
        item["action"] in {"delete", "absent"} for item in summary["prune"]
    )
    row.observation = {
        **{
            k: v
            for k, v in row.observation.items()
            if k in {"last_synced_commit", "synced_at", "reconcile_operation_id"}
        },
        "commit": commit,
        "checked_at": datetime.now(UTC).isoformat(),
        "status": "Conflict" if blocked else "OutOfSync" if changed else "InSync",
        "resources": summary["resources"],
        "retained": summary["retained"],
        "prune": summary["prune"],
    }
    session.flush()
    return review


def preview(service, principal, cluster_id, application_id, *, prune=False, allow_empty=False):
    if allow_empty and not prune:
        raise InvalidStateError("Empty applications require an explicit pruning review")
    cluster, row = application(service, principal, cluster_id, application_id)
    generation = row.generation
    inventory = dict(row.inventory)
    config = Configuration.model_validate(row.configuration)
    with GitSource(config, auth_header(row, service.secret_store)) as source:
        commit, objects = source.fetch(allow_empty=True) if allow_empty else source.fetch()
    api = api_for(service.session, service.secret_store, cluster)
    try:
        payload, summary = plan_resources(api, row, objects, prune=prune)
    except PlatformError:
        raise InvalidStateError(
            "GitOps validation failed: check namespace, ownership, schema and prune inventory. Raw responses withheld."
        ) from None
    service.session.refresh(row, with_for_update=True)
    if row.generation != generation or row.inventory != inventory:
        raise InvalidStateError("Application configuration or inventory changed; review again")
    review = persist_review(service.session, service.secret_store, row, principal.subject, commit, payload, summary)
    audit.record(
        service.session,
        principal=principal,
        action="gitops.review",
        resource_type="gitops_application",
        resource_id=row.id,
        details={"commit": commit, "review_id": str(review.id), "prune": prune, "allow_empty": allow_empty},
    )
    service.session.commit()
    return {"id": review.id, "commit": commit, "expires_at": review.expires_at, **review.summary}


def submit(service, principal, cluster_id, application_id, review_id, confirm):
    cluster, row = application(service, principal, cluster_id, application_id)
    service.session.refresh(row, with_for_update=True)
    review = service.session.scalar(
        select(GitOpsReview)
        .where(GitOpsReview.id == review_id, GitOpsReview.application_id == row.id)
        .with_for_update()
    )
    if not review or review.created_by != principal.subject:
        raise NotFoundError("GitOps review not found")
    if confirm != row.name:
        raise ConfirmationRequiredError("Repeat the application name to confirm sync")
    if review.operation_id:
        return service.operations.get(principal, review.operation_id)
    if review.generation != row.generation or review.expires_at < datetime.now(UTC):
        raise InvalidStateError("Review expired or configuration changed; review again")
    if any(
        r["action"] in {"conflict", "blocked"} for r in review.summary["resources"] + review.summary.get("prune", [])
    ):
        raise InvalidStateError("Resolve field ownership conflicts, then review again before syncing")
    busy(service.session, row)
    review.approved_at = datetime.now(UTC)
    service.session.flush()
    operation = service.operations.submit(
        principal,
        "cluster.gitops.sync",
        {
            "cluster_id": str(cluster.id),
            "cluster_name": cluster.name,
            "application_id": str(row.id),
            "review_id": str(review.id),
        },
        confirm=cluster.name,
        idempotency_key="gitops:" + str(review.id),
    )
    service.session.refresh(review)
    review.operation_id = operation.id
    service.session.commit()
    return operation


def execute(ctx):
    p = ctx.params
    automatic = not isinstance(p, SyncParams)
    review_id = uuid.UUID(ctx.outputs["review"]["review_id"]) if automatic else p.review_id
    try:
        with ctx.db() as session:
            cluster = session.get(Cluster, p.cluster_id)
            row = session.get(GitOpsApplication, p.application_id)
            review = session.get(GitOpsReview, review_id)
            if (
                not cluster
                or not row
                or not review
                or row.cluster_id != cluster.id
                or review.application_id != row.id
                or not review.approved_at
                or review.operation_id not in {None, ctx.operation_id}
                or bool(review.summary.get("automatic")) != automatic
                or cluster.lifecycle_state in {ClusterLifecycleState.DELETING, ClusterLifecycleState.DELETED}
            ):
                raise InvalidStateError("Application or reviewed source is no longer available")
            validate(row, review.generation, automatic=automatic)
            if automatic and p.generation != row.generation:
                raise InvalidStateError("Automation approval changed")
            if review.summary.get("completed"):
                ctx.set_result(review.summary["completed"])
                return review.summary["completed"]
            if review.expires_at < datetime.now(UTC):
                raise InvalidStateError("GitOps review expired before execution; review again")
            if any(
                r["action"] in {"conflict", "blocked"}
                for r in review.summary["resources"] + review.summary.get("prune", [])
            ):
                raise InvalidStateError("Ownership conflicts block this sync")
            review.operation_id = ctx.operation_id
            payload = json.loads(ctx.secret_store.unseal(SealedSecret(review.key_id, review.ciphertext)))
            objects = payload if isinstance(payload, list) else payload["apply"]
            deletions = [] if isinstance(payload, list) else payload["prune"]
            generation = row.generation
            commit = review.commit_sha
            summary = dict(review.summary)
            api = api_for(session, ctx.secret_store, cluster)

        def guard():
            ctx.check_cancelled()
            with ctx.db() as session:
                row = session.get(GitOpsApplication, p.application_id)
                cluster = session.get(Cluster, p.cluster_id)
                validate(row, generation, automatic=automatic)
                if not cluster or cluster.lifecycle_state in {
                    ClusterLifecycleState.DELETING,
                    ClusterLifecycleState.DELETED,
                }:
                    raise InvalidStateError("The cluster is being deleted")

        # All desired identities/schema and deletion identities are checked before any mutation.
        for item in objects:
            guard()
            live = resources.live_object(api, item["path"])
            resources.owned(live, p.application_id)
            marker = (live or {}).get("metadata", {}).get("annotations", {}).get(resources.REVIEW)
            if marker != str(review_id) and (
                (live["metadata"]["uid"] if live else None) != item["uid"]
                or resources.fingerprint(live) != item["fingerprint"]
            ):
                raise InvalidStateError("A target changed after review; review again")
            resources.patch(api, item["path"], item["object"], "gitops-" + str(p.application_id), dry_run=True)
        for item in deletions:
            guard()
            pruning.check_identity(api, item, p.application_id)
            with ctx.db() as session:
                row = session.get(GitOpsApplication, p.application_id)
                tracked = row.inventory.get(item["path"]) if row else None
                if tracked and tracked["uid"] != item["uid"]:
                    raise InvalidStateError("Prune inventory changed after review")
        changes = {(r["kind"], r["namespace"], r["name"]) for r in summary["resources"] if r["action"] != "unchanged"}
        applied = 0
        for index, item in enumerate(objects):
            guard()
            obj = item["object"]
            meta = obj["metadata"]
            if (obj["kind"], meta["namespace"], meta["name"]) not in changes:
                continue
            live = resources.apply_reviewed(api, item, p.application_id, review_id)
            with ctx.db() as session:
                row = session.get(GitOpsApplication, p.application_id)
                if row is None:
                    raise InvalidStateError("Application no longer exists")
                inventory = dict(row.inventory)
                inventory[item["path"]] = {
                    "kind": obj["kind"],
                    "name": meta["name"],
                    "namespace": meta["namespace"],
                    "uid": live["metadata"]["uid"],
                }
                row.inventory = inventory
            applied += 1
            ctx.set_progress((index + 1) / max(1, len(objects) + len(deletions)))
        # Applying a failing replacement must not destroy the old resources.
        if any(resources.live_object(api, item["path"]) is not None for item in deletions):
            ctx.info("Waiting for supported desired workloads to be ready before pruning")
            pruning.wait_ready(api, objects, ctx, guard)
        pruned_paths = set(summary.get("pruned_paths", []))
        for index, item in enumerate(deletions):
            guard()
            outcome = pruning.delete_owned(api, item, p.application_id, ctx, guard)
            with ctx.db() as session:
                row = session.get(GitOpsApplication, p.application_id)
                validate(row, generation, automatic=automatic)
                inventory = dict(row.inventory)
                if item["path"] in inventory and inventory[item["path"]]["uid"] == item["uid"]:
                    inventory.pop(item["path"])
                    row.inventory = inventory
                current_review = session.get(GitOpsReview, review_id)
                if current_review and outcome == "deleted":
                    pruned_paths.add(item["path"])
                    current_review.summary = {**current_review.summary, "pruned_paths": sorted(pruned_paths)}
            ctx.set_progress((len(objects) + index + 1) / max(1, len(objects) + len(deletions)))
            ctx.info(f"Prune verified for {item['kind']} {item['namespace']}/{item['name']}; outcome {outcome}")
        result = {
            "application_id": str(p.application_id),
            "commit": commit,
            "applied": applied,
            "pruned": len(pruned_paths),
            "status": "InSync" if applied == 0 and not pruned_paths else "Applied",
            "automatic": automatic,
            "notice": "Owned resources reconciled; protected resources retained. Application health remains separate.",
        }
        with ctx.db() as session:
            row = session.get(GitOpsApplication, p.application_id)
            review = session.get(GitOpsReview, review_id)
            validate(row, generation, automatic=automatic)
            assert review
            row.observation = {
                **row.observation,
                "last_synced_commit": commit,
                "synced_at": datetime.now(UTC).isoformat(),
                "status": result["status"],
            }
            review.summary = {**review.summary, "completed": result}
        ctx.info(
            f"GitOps reconciled commit {commit}: {applied} applied, {len(pruned_paths)} pruned; raw output withheld"
        )
        ctx.set_result(result)
        return result
    except PlatformError as exc:
        with ctx.db() as session:
            row = session.get(GitOpsApplication, p.application_id)
            if row and row.generation == locals().get("generation", -1):
                row.observation = {
                    **row.observation,
                    "status": "Conflict" if "conflict" in exc.message.lower() else "Error",
                    "error": "Sync stopped: check ownership, readiness, policy and operation history. "
                    "Raw responses withheld.",
                }
        raise StepFailedError(
            "GitOps sync stopped: check current policy, ownership, resource changes and workload readiness; "
            "raw responses withheld."
        ) from None


workflow_registry.register(
    WorkflowDefinition(
        type="cluster.gitops.sync",
        title="GitOps sync",
        description="Apply the encrypted commit review and explicitly reviewed guarded pruning.",
        params_model=SyncParams,
        destructive=True,
        plan=lambda _: [StepDefinition("sync", "Apply and record the reviewed Git commit", execute)],
        target=lambda p: OperationTarget("cluster", str(p.cluster_id), p.cluster_name),
    )
)
