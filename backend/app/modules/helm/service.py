"""Immutable reviewed plans and release workflows with encrypted values and revision guards."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Literal

import yaml
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import select

from app.core.errors import ConfirmationRequiredError, InvalidStateError, NotFoundError, PlatformError
from app.core.principal import Principal
from app.core.secrets import SealedSecret
from app.modules.auditing import service as audit
from app.modules.cluster_lifecycle.models import Cluster, ClusterLifecycleState
from app.modules.helm import catalog, credentials
from app.modules.helm.models import HelmPlan
from app.modules.helm.runtime import Runner, public_release
from app.modules.kubernetes import client as kc
from app.modules.kubernetes.service import KubernetesService, api_for, connection_target
from app.modules.kubernetes.service_proxy import DNSLabel
from app.modules.kubernetes.workflows import ConnectParams
from app.orchestration.context import StepContext
from app.orchestration.models import Operation
from app.orchestration.workflow import (
    OperationTarget,
    StepDefinition,
    StepFailedError,
    WorkflowDefinition,
    workflow_registry,
)

Action = Literal["install", "upgrade", "rollback", "uninstall"]
RELEASE_PATTERN = r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$"


class PlanInput(BaseModel):
    action: Action
    release: str = Field(pattern=RELEASE_PATTERN, max_length=53)
    namespace: DNSLabel
    repository: str | None = Field(default=None, pattern=catalog.NAME, max_length=63)
    chart: str | None = Field(default=None, pattern=catalog.NAME, max_length=128)
    version: str | None = Field(default=None, pattern=catalog.VERSION)
    values_yaml: SecretStr = SecretStr("")
    reuse_values: bool = True
    hooks: bool = False
    revision: int | None = Field(default=None, ge=1)
    timeout_seconds: int = Field(default=300, ge=30, le=600)


class HelmParams(ConnectParams):
    plan_id: uuid.UUID
    action: Action
    release: str = Field(pattern=RELEASE_PATTERN, max_length=53)


def repository(cluster: Cluster, name: str) -> catalog.Repository:
    row = next((r for r in (cluster.helm_repositories or []) if r["name"] == name), None)
    if not row:
        raise NotFoundError("Chart repository not found")
    return catalog.Repository.model_validate(row)


def head_uid(api: kc.ClusterAPI, namespace: str, name: str, revision: int) -> str:
    obj = kc.get_json(api, f"/api/v1/namespaces/{namespace}/secrets/sh.helm.release.v1.{name}.v{revision}")
    return obj["metadata"]["uid"]


def inventory(rendered: bytes, namespace: str) -> list[dict]:
    try:
        rows: list[dict] = []
        cluster_kinds = {
            "Namespace",
            "CustomResourceDefinition",
            "ClusterRole",
            "ClusterRoleBinding",
            "PersistentVolume",
            "StorageClass",
            "CSIDriver",
            "Node",
            "APIService",
            "PriorityClass",
            "RuntimeClass",
            "IngressClass",
            "GatewayClass",
            "MutatingWebhookConfiguration",
            "ValidatingWebhookConfiguration",
            "ValidatingAdmissionPolicy",
            "ValidatingAdmissionPolicyBinding",
            "VolumeSnapshotClass",
        }

        def add(obj: object, depth: int = 0) -> None:
            if obj is None:
                return
            if not isinstance(obj, dict) or len(rows) >= 500 or depth > 16:
                raise ValueError()
            if obj.get("kind") == "List":
                if not isinstance(obj.get("items"), list):
                    raise ValueError()
                for item in obj["items"]:
                    add(item, depth + 1)
                return
            meta = obj.get("metadata") or {}
            from app.modules.kubernetes.reserved import check_manifest

            check_manifest(obj)
            rows.append(
                {
                    "kind": str(obj.get("kind", "Unknown"))[:128],
                    "name": str(meta.get("name", "(generated)"))[:253],
                    "namespace": str(
                        meta.get("namespace", "(cluster)" if obj.get("kind") in cluster_kinds else namespace)
                    )[:63],
                }
            )

        for obj in yaml.safe_load_all(rendered):
            add(obj)
        return rows
    except (yaml.YAMLError, ValueError, TypeError, RecursionError, AttributeError):
        raise InvalidStateError("Chart rendered an unsupported or oversized resource inventory") from None


def preview(service: KubernetesService, principal: Principal, cluster_id: uuid.UUID, body: PlanInput) -> dict:
    cluster, api = service.api(principal, cluster_id)
    # Require an existing namespace: namespace creation remains an explicit resource operation.
    kc.get_json(api, f"/api/v1/namespaces/{body.namespace}")
    values = body.values_yaml.get_secret_value()
    catalog.values(values)
    target = connection_target(service.session, service.secret_store, cluster)
    spec = {
        key: value for key, value in body.model_dump(exclude={"values_yaml", "repository", "chart", "version"}).items()
    }
    with Runner(target) as helm:
        current = helm.release(body.namespace, body.release)
        if body.action == "install" and current:
            raise InvalidStateError("This release already exists; choose upgrade")
        if body.action != "install" and not current:
            raise NotFoundError("Helm release not found")
        if current and str(current["status"]).startswith("pending") and body.action != "uninstall":
            raise InvalidStateError("This release has a pending Helm action; inspect it before another change")
        spec["expected_revision"] = int(current["revision"]) if current else 0
        spec["expected_uid"] = (
            head_uid(api, body.namespace, body.release, spec["expected_revision"]) if current else None
        )
        if body.action == "rollback":
            revisions = helm.history(body.namespace, body.release)
            selected = next((r for r in revisions if int(r["revision"]) == body.revision), None)
            if (
                body.revision is None
                or not selected
                or selected["status"] not in {"deployed", "superseded"}
                or body.revision >= spec["expected_revision"]
            ):
                raise InvalidStateError("Choose an available successful revision older than the current revision")
    resources = []
    if body.action in {"install", "upgrade"}:
        if not body.repository or not body.chart or not body.version:
            raise InvalidStateError("Select a repository, chart and explicit version")
        repo = repository(cluster, body.repository)
        auth = credentials.authorization(service.session, service.secret_store, cluster, repo)
        spec["source"] = (
            catalog.chart(repo, body.chart, body.version, auth)
            if auth
            else catalog.chart(repo, body.chart, body.version)
        )
        bundle = catalog.archive(spec["source"], auth) if auth else catalog.archive(spec["source"])
        # Offline rendering never runs chart hooks or uses cluster credentials.
        with Runner() as helm:
            archive_path = helm.write("chart.tgz", bundle)
            values_path = helm.write("values.yaml", values.encode())
            args = [
                "template",
                body.release,
                str(archive_path),
                "--namespace",
                body.namespace,
                "--values",
                str(values_path),
                "--include-crds",
            ]
            if cluster.kubernetes_version:
                args += ["--kube-version", cluster.kubernetes_version]
            if body.action == "upgrade":
                args += ["--is-upgrade"]
            if not body.hooks:
                args += ["--no-hooks"]
            resources = inventory(helm.run(args), body.namespace)
    sealed = service.secret_store.seal(values.encode())
    row = HelmPlan(
        organization_id=cluster.organization_id,
        project_id=cluster.project_id,
        cluster_id=cluster.id,
        created_by=principal.subject,
        specification=spec,
        key_id=sealed.key_id,
        ciphertext=sealed.ciphertext,
        expires_at=datetime.now(UTC) + timedelta(minutes=15),
    )
    service.session.add(row)
    service.session.flush()
    audit.record(
        service.session,
        principal=principal,
        action="helm.preview",
        resource_type="cluster",
        resource_id=cluster.id,
        details={"action": body.action, "release": body.release, "namespace": body.namespace, "plan_id": str(row.id)},
    )
    service.session.commit()
    return {
        "id": row.id,
        "expires_at": row.expires_at,
        "specification": spec,
        "resources": resources,
        "notice": "Resource names/kinds only; values, Secret contents and rendered manifests are withheld. "
        "Upgrades using existing values may render differently at execution.",
    }


def submit(
    service: KubernetesService, principal: Principal, cluster_id: uuid.UUID, plan_id: uuid.UUID, confirm: str | None
) -> Operation:
    cluster = service.cluster(principal, cluster_id)
    if cluster.lifecycle_state in {ClusterLifecycleState.DELETING, ClusterLifecycleState.DELETED}:
        raise InvalidStateError("The cluster is being deleted")
    row = service.session.scalar(
        select(HelmPlan).where(HelmPlan.id == plan_id, HelmPlan.cluster_id == cluster_id).with_for_update()
    )
    if row is None or row.created_by != principal.subject:
        raise NotFoundError("Reviewed Helm plan not found")
    spec = row.specification
    if spec["action"] != "install" and confirm != spec["release"]:
        raise ConfirmationRequiredError("Repeat the release name to confirm this change")
    if row.operation_id:
        return service.operations.get(principal, row.operation_id)
    if row.expires_at < datetime.now(UTC):
        raise InvalidStateError("Helm review expired; preview the change again")
    row.approved_at = datetime.now(UTC)
    op = service.operations.submit(
        principal,
        "cluster.helm." + spec["action"],
        {
            "cluster_id": str(cluster.id),
            "cluster_name": cluster.name,
            "plan_id": str(row.id),
            "action": spec["action"],
            "release": spec["release"],
        },
        confirm=cluster.name if spec["action"] != "install" else None,
        idempotency_key=f"helm:{row.id}",
    )
    service.session.refresh(row)
    row.operation_id = op.id
    service.session.commit()
    return op


def execute(ctx: StepContext) -> dict:
    p: HelmParams = ctx.params  # type: ignore[assignment]
    return execute_plan(ctx, p)


def execute_plan(ctx: StepContext, p: HelmParams, before_mutation: Callable[[], None] | None = None) -> dict:
    ctx.check_cancelled()
    with ctx.db() as session:
        cluster = session.get(Cluster, p.cluster_id)
        plan = session.scalar(select(HelmPlan).where(HelmPlan.id == p.plan_id).with_for_update())
        if (
            cluster is None
            or cluster.lifecycle_state in {ClusterLifecycleState.DELETING, ClusterLifecycleState.DELETED}
            or plan is None
            or plan.cluster_id != p.cluster_id
            or not plan.approved_at
            or plan.operation_id not in {None, ctx.operation_id}
            or plan.specification["action"] != p.action
            or plan.specification["release"] != p.release
        ):
            raise StepFailedError("Reviewed Helm plan is missing, unapproved, or belongs to another action")
        plan.operation_id = ctx.operation_id
        spec = dict(plan.specification)
        expires_at = plan.expires_at
        values = ctx.secret_store.unseal(SealedSecret(plan.key_id, plan.ciphertext))
        target = connection_target(session, ctx.secret_store, cluster)
        api = api_for(session, ctx.secret_store, cluster)
    marker = f"kplatform:{ctx.operation_id}"
    ns, release = spec["namespace"], spec["release"]
    try:
        with Runner(target, ctx) as helm:
            current = helm.release(ns, release)
            if p.action == "uninstall" and current is None:
                result = {"action": p.action, "release": release, "namespace": ns, "status": "absent"}
                ctx.set_result(result)
                return result
            if current and int(current["revision"]) == spec["expected_revision"] + 1:
                history = helm.history(ns, release)
                if any(
                    int(r["revision"]) == int(current["revision"]) and r.get("description") == marker for r in history
                ):
                    if current["status"] == "deployed":
                        ctx.info("This operation's Helm revision is already deployed; no action was repeated")
                        result = {**public_release(current), "action": p.action}
                        ctx.set_result(result)
                        return result
                    raise StepFailedError(
                        "This operation left a non-deployed Helm revision; inspect it before a new review"
                    )
            if expires_at < datetime.now(UTC):
                raise StepFailedError("Helm review expired before execution; create a new preview")
            if spec["expected_revision"] == 0:
                if current:
                    raise StepFailedError("Release appeared after review; refusing to overwrite it")
            else:
                if (
                    not current
                    or int(current["revision"]) != spec["expected_revision"]
                    or head_uid(api, ns, release, spec["expected_revision"]) != spec["expected_uid"]
                ):
                    raise StepFailedError("Release identity or revision changed after review; preview again")
            args = [p.action, release]
            if p.action in {"install", "upgrade"}:
                with ctx.db() as session:
                    source_cluster = session.get(Cluster, p.cluster_id)
                    assert source_cluster is not None
                    auth = credentials.source_authorization(session, ctx.secret_store, source_cluster, spec["source"])
                bundle = catalog.archive(spec["source"], auth) if auth else catalog.archive(spec["source"])
                args += [
                    str(helm.write("chart.tgz", bundle)),
                    "--values",
                    str(helm.write("values.yaml", values)),
                    "--description",
                    marker,
                    "--rollback-on-failure",
                    "--hide-notes",
                ]
                if p.action == "upgrade":
                    args += ["--reuse-values" if spec["reuse_values"] else "--reset-values", "--history-max", "50"]
            elif p.action == "rollback":
                args += [str(spec["revision"]), "--description", marker, "--history-max", "50"]
            elif p.action == "uninstall":
                args += ["--cascade", "foreground"]
            args += ["--namespace", ns, "--timeout", f"{spec['timeout_seconds']}s", "--wait"]
            if not spec["hooks"]:
                args += ["--no-hooks"]
            ctx.check_cancelled()
            if p.action in {"install", "upgrade"}:
                with ctx.db() as session:
                    source_cluster = session.get(Cluster, p.cluster_id)
                    assert source_cluster is not None
                    credentials.source_authorization(session, ctx.secret_store, source_cluster, spec["source"])
            if expires_at < datetime.now(UTC):
                raise StepFailedError("Helm review expired while preparing the chart; preview again")
            # Downloads can be slow. Recheck immediately before Helm can change resources.
            latest = helm.release(ns, release)
            if spec["expected_revision"] == 0:
                if latest:
                    raise StepFailedError("Release appeared while preparing the chart; preview again")
            elif (
                not latest
                or int(latest["revision"]) != spec["expected_revision"]
                or head_uid(api, ns, release, spec["expected_revision"]) != spec["expected_uid"]
            ):
                raise StepFailedError("Release changed while preparing the action; preview again")
            ctx.info(f"Executing Helm {p.action} for {ns}/{release}; values and raw Helm output are withheld")
            if before_mutation:
                before_mutation()
            helm.run(args, timeout=spec["timeout_seconds"] + 30)
            current = helm.release(ns, release)
            if p.action == "uninstall":
                if current:
                    raise StepFailedError("Helm release is still present after uninstall")
                result = {"action": p.action, "release": release, "namespace": ns, "status": "absent"}
            else:
                if not current or current["status"] != "deployed":
                    raise StepFailedError("Helm did not report a deployed release after waiting")
                result = {**public_release(current), "action": p.action}
            ctx.set_result(result)
            return result
    except PlatformError as exc:
        raise StepFailedError(exc.message, code=exc.code) from None


for action in ("install", "upgrade", "rollback", "uninstall"):
    workflow_registry.register(
        WorkflowDefinition(
            type="cluster.helm." + action,
            title=f"Helm {action}",
            description="Execute the reviewed, revision-guarded Helm release plan.",
            params_model=HelmParams,
            destructive=action != "install",
            title_for=lambda p: f"Helm {p.action}: {p.release}",
            plan=lambda _: [StepDefinition("execute", "Execute and verify the reviewed Helm action", execute)],
            target=lambda p: OperationTarget("cluster", str(p.cluster_id), p.cluster_name),
        )
    )
