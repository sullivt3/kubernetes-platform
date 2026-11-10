"""Workflow steps shared by cloud managed Kubernetes distributions."""

from __future__ import annotations

import ipaddress
from typing import Any

import yaml
from sqlalchemy import select

from app.core.principal import Principal
from app.modules.cluster_lifecycle.distributions.base import ClusterPlanInput
from app.modules.cluster_lifecycle.models import Cluster
from app.modules.credentials.models import Credential, CredentialKind
from app.modules.credentials.service import CredentialService
from app.modules.infrastructure.models import ProviderConnection
from app.modules.infrastructure.provider_workflows import provider_for
from app.modules.providers.types import ManagedClusterRequest
from app.orchestration.context import StepContext
from app.orchestration.workflow import StepFailedError


def _plan(ctx: StepContext) -> ClusterPlanInput:
    return ctx.params  # type: ignore[return-value]


def create(ctx: StepContext) -> dict[str, Any]:
    plan = _plan(ctx)
    connection_id = plan.spec.provider_connection_id
    if connection_id is None:
        raise StepFailedError("Managed cluster creation needs a cloud provider connection")
    with ctx.db() as session:
        registered = session.get(Cluster, plan.cluster_id)
        persisted_managed = ((registered.spec or {}).get("options", {}).get("managed") or {}) if registered else {}
    cloud_name = str(
        persisted_managed.get("cluster_name") or f"{plan.cluster_name[:26].rstrip('-')}-{plan.cluster_id.hex[:12]}"
    )
    options = {
        **plan.spec.options,
        "managed_cni": plan.spec.network.cni.value,
        "managed_pod_cidr": plan.spec.network.pod_cidr,
        "managed_service_cidr": plan.spec.network.service_cidr,
        "managed_dns_service_ip": str(ipaddress.ip_network(plan.spec.network.service_cidr).network_address + 10),
    }
    node_pools = [
        {
            "name": pool.name,
            "count": pool.count,
            "minimum": int(options.get("managed_min_node_count", 1)),
            "maximum": max(int(options.get("managed_max_node_count", max(3, pool.count))), pool.count),
            "disk_gb": pool.machine_template.disk_gb if pool.machine_template else 20,
            "machine_type": (
                pool.machine_template.provider_options.get("machine_type") if pool.machine_template else None
            ),
            "labels": pool.labels,
            "taints": [taint.model_dump(mode="json", by_alias=True) for taint in pool.taints],
        }
        for pool in plan.spec.worker_pools
        if pool.count > 0
    ]
    request = ManagedClusterRequest(
        name=cloud_name,
        version=plan.spec.kubernetes_version,
        options=options,
        node_pools=node_pools,
    )
    with provider_for(ctx, connection_id) as provider:
        external_id = provider.managed_cluster_id(cloud_name, options)
        with ctx.db() as session:
            cluster = session.get(Cluster, plan.cluster_id)
            if cluster is None:
                raise StepFailedError("Cluster registration was deleted before provider provisioning")
            persisted = dict(cluster.spec or {})
            persisted_options = dict(persisted.get("options") or {})
            persisted_options["managed"] = {"cluster_name": cloud_name, "external_id": external_id}
            persisted["options"] = persisted_options
            cluster.spec = persisted
        created = provider.create_managed_cluster(request)
    if created.external_id != external_id:
        raise StepFailedError("Provider returned an unexpected managed cluster ID")
    if not created.endpoint.startswith("https://") or not created.certificate_authority_data:
        raise StepFailedError("The provider returned an incomplete managed Kubernetes API endpoint")

    # Providers with renewable cloud identity use a marker that KubeClient replaces on each
    # request. AKS can return a client certificate pair, which stays encrypted in the kubeconfig.
    user = (
        {
            "client-certificate-data": created.client_certificate_data,
            "client-key-data": created.client_key_data,
        }
        if created.client_certificate_data and created.client_key_data
        else {"token": "managed-auth-placeholder"}
    )
    kubeconfig = yaml.safe_dump(
        {
            "apiVersion": "v1",
            "kind": "Config",
            "current-context": plan.cluster_name,
            "clusters": [
                {
                    "name": plan.cluster_name,
                    "cluster": {
                        "server": created.endpoint,
                        "certificate-authority-data": created.certificate_authority_data,
                    },
                }
            ],
            "users": [{"name": "platform", "user": user}],
            "contexts": [{"name": plan.cluster_name, "context": {"cluster": plan.cluster_name, "user": "platform"}}],
        },
        sort_keys=False,
    )
    principal = Principal.system(ctx.organization_id, "managed-cluster-provisioner")
    with ctx.db() as session:
        connection = session.get(ProviderConnection, connection_id)
        cluster = session.get(Cluster, plan.cluster_id)
        if connection is None or cluster is None:
            raise StepFailedError("The provider connection or cluster registration was deleted")
        name = f"cluster-{plan.cluster_name}-managed-access"
        service = CredentialService(session, ctx.secret_store)
        existing = session.scalar(
            select(Credential).where(Credential.organization_id == ctx.organization_id, Credential.name == name)
        )
        credential = (
            service.replace_secret(principal, existing.id, kubeconfig)
            if existing
            else service.create(
                principal,
                name=name,
                kind=CredentialKind.KUBECONFIG,
                secret_value=kubeconfig,
                description=f"Provider-managed API access for {plan.cluster_name}",
                public_metadata={"cluster": plan.cluster_name, "provider": connection.provider_type.value},
            )
        )
        cluster.access_credential_id = credential.id
        cluster.api_endpoint = created.endpoint
        cluster.kubernetes_version = "v" + (
            ".".join(plan.spec.kubernetes_version.removeprefix("v").split(".")[:2])
            if plan.spec.distribution.value == "eks"
            else plan.spec.kubernetes_version.removeprefix("v")
        )
        persisted = dict(cluster.spec or {})
        persisted_options = dict(persisted.get("options") or {})
        persisted_options["managed"] = {"cluster_name": cloud_name, "external_id": external_id}
        persisted["options"] = persisted_options
        cluster.spec = persisted
    ctx.info(f"Managed Kubernetes API is ready at {created.endpoint}")
    return {"external_id": created.external_id, "endpoint": created.endpoint}


def delete(ctx: StepContext) -> dict[str, Any]:
    plan = _plan(ctx)
    managed = plan.spec.options.get("managed") or {}
    external_id = managed.get("external_id")
    connection_id = plan.spec.provider_connection_id
    if not external_id or connection_id is None:
        return {"deleted": False, "reason": "managed cluster was not provisioned"}
    with provider_for(ctx, connection_id) as provider:
        provider.delete_managed_cluster(str(external_id))
    ctx.info("Provider-managed Kubernetes cluster deleted")
    return {"deleted": True}


def resize(ctx: StepContext) -> dict[str, Any]:
    plan = _plan(ctx)
    external_id = (plan.spec.options.get("managed") or {}).get("external_id")
    if not external_id or not plan.spec.provider_connection_id or not plan.pool_name or plan.pool_count is None:
        raise StepFailedError("Managed pool operation is missing its provider identifiers")
    with provider_for(ctx, plan.spec.provider_connection_id) as provider:
        provider.resize_managed_pool(str(external_id), plan.pool_name, plan.pool_count)
    with ctx.db() as session:
        cluster = session.get(Cluster, plan.cluster_id)
        if cluster is None:
            raise StepFailedError("Cluster registration no longer exists")
        persisted = dict(cluster.spec)
        pools = [dict(pool) for pool in persisted.get("worker_pools", [])]
        for pool in pools:
            if pool["name"] == plan.pool_name:
                pool["count"] = plan.pool_count
        persisted["worker_pools"] = pools
        cluster.spec = persisted
    return {"pool": plan.pool_name, "count": plan.pool_count}


def upgrade(ctx: StepContext) -> dict[str, Any]:
    plan = _plan(ctx)
    external_id = (plan.spec.options.get("managed") or {}).get("external_id")
    if not external_id or not plan.spec.provider_connection_id or not plan.target_version:
        raise StepFailedError("Managed upgrade is missing its provider identifiers")
    with provider_for(ctx, plan.spec.provider_connection_id) as provider:
        provider.upgrade_managed_cluster(str(external_id), plan.target_version)
    return {"version": plan.target_version}
