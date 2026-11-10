"""Persist installer credentials idempotently without putting keys in step outputs."""

from typing import Any

from sqlalchemy import select

from app.core.principal import Principal
from app.modules.cluster_lifecycle.distributions.base import ClusterPlanInput
from app.modules.cluster_lifecycle.models import Cluster, ClusterMachine, MachineMembershipState
from app.modules.credentials.models import Credential, CredentialKind
from app.modules.credentials.service import CredentialService
from app.modules.kubernetes.kubeconfig import resolve
from app.orchestration.context import StepContext
from app.orchestration.workflow import StepFailedError


def save(ctx: StepContext, plan: ClusterPlanInput, kubeconfig: str, version: str) -> dict[str, Any]:
    resolved = resolve(kubeconfig)
    ctx.redact(kubeconfig, resolved.client_key_pem or "", resolved.token or "")
    principal = Principal.system(ctx.organization_id, "cluster-installer")
    name = f"cluster-{plan.cluster_id}-admin"
    with ctx.db() as session:
        cluster = session.get(Cluster, plan.cluster_id)
        if cluster is None or cluster.organization_id != ctx.organization_id:
            raise StepFailedError("Cluster registration no longer exists")
        existing = session.scalar(
            select(Credential).where(
                Credential.organization_id == ctx.organization_id,
                Credential.name == name,
            )
        )
        service = CredentialService(session, ctx.secret_store)
        credential = (
            service.replace_secret(principal, existing.id, kubeconfig)
            if existing
            else service.create(
                principal,
                name=name,
                kind=CredentialKind.KUBECONFIG,
                secret_value=kubeconfig,
                description=f"Installer access for {plan.cluster_name}",
                public_metadata={"cluster_id": str(plan.cluster_id)},
            )
        )
        cluster.access_credential_id = credential.id
        cluster.api_endpoint = resolved.server
        cluster.kubernetes_version = version
        for membership in session.scalars(select(ClusterMachine).where(ClusterMachine.cluster_id == plan.cluster_id)):
            membership.membership_state = MachineMembershipState.JOINED
    return {"endpoint": resolved.server, "version": version}
