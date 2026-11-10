"""Regression coverage for overdue distribution/provider paths, using explicit simulators."""

from __future__ import annotations

import base64
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlsplit

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.modules.cluster_lifecycle.distributions import local_steps, rancher_steps
from app.modules.cluster_lifecycle.distributions.base import ClusterPlanInput, PlannedNode
from app.modules.cluster_lifecycle.distributions.registry import distribution_registry
from app.modules.cluster_lifecycle.distributions.spec import ClusterSpec, StorageSpec
from app.modules.cluster_lifecycle.distributions.storage_steps import plan_storage
from app.modules.infrastructure.host_access import CommandResult
from app.modules.providers import http
from app.modules.providers.implementations.local import LocalProvider
from app.modules.providers.implementations.vsphere import OWNER
from app.modules.providers.registry import provider_registry
from app.modules.providers.types import (
    ConnectionCheckResult,
    MachineRef,
    ManagedClusterRequest,
    NetworkConfigRequest,
    PowerAction,
    ProviderType,
)
from tests.fake_kube import kubeconfig_text
from tests.test_infrastructure_providers import _aws, _instance, _instances_xml, _json
from tests.test_infrastructure_providers import transport as transport

SIZE = {"cpu_cores": 2, "memory_mb": 4096, "disk_gb": 40}


def plan(distribution: str, cni: str | None = None) -> ClusterPlanInput:
    dtype = next(d for d in distribution_registry.all() if d.distribution_type.value == distribution)
    spec = ClusterSpec.model_validate(
        {
            "name": "overdue",
            "distribution": distribution,
            "provider_type": "local" if distribution in {"kind", "k3d"} else "ssh",
            "kubernetes_version": "1.34.2",
            "network": {"cni": cni or dtype.default_cni.value},
            "control_plane": {"count": 1, "machine_template": SIZE},
            "worker_pools": [
                {
                    "name": "workers",
                    "count": 1,
                    "machine_template": SIZE,
                    "labels": {"tier": "apps"},
                    "taints": [{"key": "dedicated", "effect": "NoSchedule"}],
                }
            ],
        }
    )
    cp = PlannedNode(machine_id=uuid.uuid4(), name="cp-1", role="control_plane", pool="control-plane")
    worker = PlannedNode(machine_id=uuid.uuid4(), name="worker-1", role="worker", pool="workers")
    return ClusterPlanInput(cluster_id=uuid.uuid4(), cluster_name=spec.name, spec=spec, nodes=[cp, worker])


@pytest.mark.parametrize("distribution", ["kind", "k3d", "k3s", "rke2"])
def test_overdue_distributions_have_real_create_delete_plans(distribution: str) -> None:
    p = plan(distribution)
    distro = distribution_registry.get(p.spec.distribution)
    assert distro.validate_spec(p.spec) == []
    assert distro.implementation_status.value == "preview"
    assert distro.plan_create(p) and distro.plan_delete(p)
    if distribution in {"k3s", "rke2"}:
        assert [s.key for s in distro.plan_create(p)][:3] == ["wait-machines", "validate", "prepare"]
        assert distro.plan_add_nodes(p) and distro.plan_remove_nodes(p) and distro.plan_upgrade(p, "1.35.1")


@pytest.mark.parametrize("distribution", ["kind", "k3s"])
def test_cilium_has_an_executable_step(distribution: str) -> None:
    p = plan(distribution, "cilium")
    assert distro_steps(p)["cni"].fn.__module__.endswith("cni_steps")


def distro_steps(p):
    return {s.key: s for s in distribution_registry.get(p.spec.distribution).plan_create(p)}


def test_storage_validates_inputs_and_appends_real_step() -> None:
    with pytest.raises(ValidationError):
        StorageSpec(csi_drivers=["unknown"])
    with pytest.raises(ValidationError):
        StorageSpec(nfs_server="10.0.0.1")
    p = plan("kind")
    assert plan_storage(p) == []
    p.spec.storage = StorageSpec(
        csi_drivers=["nfs.csi.k8s.io"], default_storage_class="nfs", nfs_server="nfs.internal", nfs_share="/exports"
    )
    assert plan_storage(p)[0].key == "storage"


def test_native_upgrade_revision_is_independent_of_creation_revision():
    p = plan("rke2")
    p.spec.options["release_revision"] = 3
    assert rancher_steps.release(p) == "v1.34.2+rke2r3"
    assert rancher_steps.release(p, "1.35.1") == "v1.35.1+rke2r1"
    p.target_release_revision = 2
    assert rancher_steps.release(p, "1.35.1") == "v1.35.1+rke2r2"


@pytest.mark.parametrize(("distribution", "expected"), [("k3s", "v1.34.2+k3s1"), ("rke2", "v1.34.2+rke2r1")])
def test_native_installers_pin_release_and_send_join_secrets_on_stdin(monkeypatch, distribution, expected):
    p = plan(distribution)
    commands = []
    redacted = []

    class Host:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def run(self, value, options=None):
            return CommandResult(value, 1 if "cat /etc/rancher" in value else 0, "", "", 0)

    monkeypatch.setattr(rancher_steps, "connect", lambda *_: Host())

    def execute(executor, script, **kwargs):
        commands.append((script, kwargs))
        return CommandResult("bash -s", 0, "installed", "", 0)

    monkeypatch.setattr(rancher_steps, "run_script", execute)
    ctx = SimpleNamespace(redact=lambda *values: redacted.extend(values), info=lambda _: None)
    rancher_steps.install_node(ctx, p, p.workers[0], token="private-join-token", server="https://10.0.0.1:9345")
    script, options = commands[0]
    assert expected in script and options["env"][f"INSTALL_{distribution.upper()}_VERSION"] == expected
    assert "private-join-token" in redacted and "private-join-token" not in script
    encoded = script.split("printf '%s' '", 1)[1].split("'", 1)[0]
    config = yaml.safe_load(base64.b64decode(encoded))
    assert config["token"] == "private-join-token" and config["server"] == "https://10.0.0.1:9345"
    assert config["node-label"] == ["kplatform.io/pool=workers", "tier=apps"]
    assert config["node-taint"] == ["dedicated=:NoSchedule"]


@pytest.mark.parametrize("distribution", ["kind", "k3d"])
def test_local_cluster_api_needs_no_ssh_machine_provisioning(client: TestClient, monkeypatch, distribution):
    calls = []
    configs = []
    monkeypatch.setattr(
        LocalProvider, "validate_connection", lambda _: ConnectionCheckResult(ok=True, message="fake Docker")
    )
    monkeypatch.setattr(local_steps, "environment", lambda *_: {})

    def run(ctx, tool, args, env, **kwargs):
        calls.append(args)
        if args[:2] == ["get", "clusters"]:
            return ""
        if args[:2] == ["cluster", "list"]:
            return "[]"
        if "--config" in args:
            configs.append(yaml.safe_load(Path(args[args.index("--config") + 1]).read_text()))
        if args[:2] in (["get", "kubeconfig"], ["kubeconfig", "get"]):
            return kubeconfig_text()
        return ""

    monkeypatch.setattr(local_steps, "run", run)
    monkeypatch.setattr(local_steps, "ready", lambda _: {"nodes": ["simulated-node"]})
    conn = client.post("/api/v1/provider-connections", json={"name": "local", "provider_type": "local", "config": {}})
    assert conn.status_code == 201, conn.text
    p = plan(distribution)
    p.spec.provider_connection_id = uuid.UUID(conn.json()["connection"]["id"])
    body = {"spec": p.spec.model_dump(mode="json")}
    preview = client.post("/api/v1/clusters/validate", json=body)
    assert preview.json()["valid"], preview.text
    assert preview.json()["nodes"] == []
    created = client.post("/api/v1/clusters", json=body)
    assert created.status_code == 202, created.text
    result = created.json()
    assert result["operation"]["status"] == "succeeded", result
    cluster_id = result["cluster"]["id"]
    cluster = client.get(f"/api/v1/clusters/{cluster_id}").json()
    assert cluster["has_kubeconfig"] and cluster["nodes"] == []
    assert client.get("/api/v1/machines").json() == []
    assert configs and any("create" in args for args in calls)
    deleted = client.delete(f"/api/v1/clusters/{cluster_id}", params={"confirm": "overdue"})
    assert deleted.json()["status"] == "succeeded", deleted.text
    assert client.get(f"/api/v1/clusters/{cluster_id}").status_code == 404


@pytest.mark.parametrize("owned", [True, False])
def test_aws_power_and_network_refuse_unowned_instances(transport, owned):
    actions = []

    def api(request):
        params = dict(parse_qsl((request.body or b"").decode()))
        actions.append(params["Action"])
        if params["Action"] == "DescribeInstances":
            return http.HttpResponse(
                200,
                {},
                _instances_xml(
                    _instance("i-owned", "vm", "running", "10.0.0.8", managed=owned).replace(
                        "<vpcId>", "<subnetId>subnet-a</subnetId><vpcId>"
                    )
                ),
            )
        if params["Action"] in {"StopInstances", "ModifyInstanceAttribute"}:
            if params["Action"] == "ModifyInstanceAttribute":
                assert params["GroupId.1"] == "sg-a"
            return http.HttpResponse(200, {}, b"<Response/>")
        return None

    transport.append(api)
    provider = _aws()
    ref = MachineRef(external_id="eu-west-1/i-owned")
    if not owned:
        with pytest.raises(http.ProviderApiError, match="ownership"):
            provider.set_power_state(ref, PowerAction.STOP)
        with pytest.raises(http.ProviderApiError, match="ownership"):
            provider.configure_network(
                ref, NetworkConfigRequest(network="subnet-a", options={"security_group_ids": ["sg-a"]})
            )
        assert set(actions) == {"DescribeInstances"}
    else:
        provider.set_power_state(ref, PowerAction.STOP)
        provider.configure_network(
            ref, NetworkConfigRequest(network="subnet-a", options={"security_group_ids": ["sg-a"]})
        )
        assert {"StopInstances", "ModifyInstanceAttribute"} <= set(actions)


def test_gke_uses_cluster_wrapper_native_cilium_and_done_status(transport, monkeypatch):
    from app.modules.providers.implementations.gcp import GcpProvider

    key = json.dumps(
        {"project_id": "project", "client_email": "test@project.iam.gserviceaccount.com", "private_key": "not-used"}
    ).encode()
    provider = provider_registry.create(ProviderType.GCP, {"project_id": "project"}, key)
    monkeypatch.setattr(GcpProvider, "_access_token", lambda *_: "test-token")
    state = {"created": False}

    def api(request):
        path = urlsplit(request.url).path
        if path.endswith("/operations/op1"):
            return _json(200, {"status": "DONE"})
        if path.endswith("/clusters") and request.method == "POST":
            body = json.loads(request.body)["cluster"]
            assert body["networkConfig"]["datapathProvider"] == "ADVANCED_DATAPATH"
            assert body["nodePools"][0]["config"]["diskSizeGb"] == 60
            assert body["nodePools"][0]["config"]["taints"][0]["effect"] == "PREFER_NO_SCHEDULE"
            assert "removeDefaultNodePool" not in body
            state["created"] = True
            return _json(200, {"name": "op1"})
        if path.endswith("/clusters/prod"):
            return (
                _json(
                    200,
                    {
                        "status": "RUNNING",
                        "endpoint": "1.2.3.4",
                        "resourceLabels": {"kubernetes-platform-managed": "true"},
                        "masterAuth": {"clusterCaCertificate": "Y2E="},
                    },
                )
                if state["created"]
                else _json(404, {})
            )
        return None

    transport.append(api)
    result = provider.create_managed_cluster(
        ManagedClusterRequest(
            name="prod",
            version="1.34.2",
            options={
                "managed_location": "us-central1-a",
                "managed_network": "default",
                "managed_subnetwork": "default",
                "managed_cni": "cilium",
            },
            node_pools=[
                {
                    "name": "workers",
                    "count": 1,
                    "disk_gb": 60,
                    "taints": [{"key": "dedicated", "effect": "PreferNoSchedule"}],
                }
            ],
        )
    )
    assert result.endpoint == "https://1.2.3.4"


def test_vsphere_mutations_require_ownership(monkeypatch):
    provider = provider_registry.create(
        ProviderType.VSPHERE, {"server": "vc", "username": "ops", "datacenter": "dc"}, b"pw"
    )
    vm = SimpleNamespace(config=SimpleNamespace(extraConfig=[]))
    monkeypatch.setattr(provider, "_vm", lambda _: vm)
    with pytest.raises(http.ProviderApiError, match="ownership"):
        provider.delete(MachineRef(external_id="uuid"))
    vm.config.extraConfig = [SimpleNamespace(key=OWNER, value="true")]
    vm.runtime = SimpleNamespace(powerState="poweredOff")
    tasks = []
    vm.Destroy_Task = lambda: SimpleNamespace(info=SimpleNamespace(state="success", result=tasks.append("destroy")))
    provider.delete(MachineRef(external_id="uuid"))
    assert tasks == ["destroy"]


def test_managed_pool_resize_is_confirmed_tracked_and_persisted(client, session, principal, monkeypatch):
    from contextlib import contextmanager

    from app.modules.cluster_lifecycle.distributions import managed_steps
    from app.modules.cluster_lifecycle.models import Cluster, ClusterLifecycleState, ClusterOrigin
    from app.modules.infrastructure.models import ProviderConnection

    connection = ProviderConnection(
        organization_id=principal.organization_id, name="aws", provider_type=ProviderType.AWS, config={}
    )
    session.add(connection)
    session.flush()
    spec = ClusterSpec.model_validate(
        {
            "name": "managed",
            "distribution": "eks",
            "provider_type": "aws",
            "provider_connection_id": str(connection.id),
            "kubernetes_version": "1.34",
            "network": {"cni": "provider-default"},
            "control_plane": {"count": 0},
            "worker_pools": [{"name": "workers", "count": 2, "machine_template": SIZE}],
            "options": {"managed": {"external_id": "eu-west-1|managed"}},
        }
    )
    cluster = Cluster(
        organization_id=principal.organization_id,
        name="managed",
        distribution=spec.distribution,
        origin=ClusterOrigin.PROVISIONED,
        lifecycle_state=ClusterLifecycleState.ACTIVE,
        provider_connection_id=connection.id,
        spec=spec.model_dump(mode="json"),
        kubernetes_version="v1.34.2-eks.test",
    )
    session.add(cluster)
    session.commit()
    calls = []

    @contextmanager
    def provider(*_):
        yield SimpleNamespace(
            resize_managed_pool=lambda *args: calls.append(args),
            upgrade_managed_cluster=lambda *args: calls.append(args),
        )

    monkeypatch.setattr(managed_steps, "provider_for", provider)
    path = f"/api/v1/clusters/{cluster.id}/pools/workers/resize"
    refused = client.post(path, json={"count": 1})
    assert refused.status_code == 428 and calls == [], refused.text
    response = client.post(path, json={"count": 1}, params={"confirm": "managed"})
    assert response.status_code == 202 and response.json()["status"] == "succeeded", response.text
    assert calls == [("eu-west-1|managed", "workers", 1)]
    assert client.get(f"/api/v1/clusters/{cluster.id}").json()["spec"]["worker_pools"][0]["count"] == 1
    refused_upgrade = client.post(f"/api/v1/clusters/{cluster.id}/upgrade", json={"target_version": "1.36"})
    assert refused_upgrade.status_code == 422
    upgraded = client.post(f"/api/v1/clusters/{cluster.id}/upgrade", json={"target_version": "1.35"})
    assert upgraded.status_code == 202 and upgraded.json()["status"] == "succeeded", upgraded.text
    assert calls[-1] == ("eu-west-1|managed", "1.35")
