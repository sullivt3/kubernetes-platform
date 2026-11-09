"""Workflow steps for kubeadm clusters: create, add nodes, remove nodes, delete.

Every step is idempotent (scripts check state before acting) so the engine can
resume after a crash or a manual retry. Secrets generated here (bootstrap tokens,
certificate keys) are created per step, registered for log redaction, passed to
nodes over stdin and never stored in step outputs.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import secrets
import shlex
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from functools import cache
from importlib import resources
from typing import Any

from sqlalchemy import delete, select

from app.core.principal import Principal
from app.modules.cluster_lifecycle.distributions.base import ClusterPlanInput, PlannedNode
from app.modules.cluster_lifecycle.distributions.kubeadm import config as cfg
from app.modules.cluster_lifecycle.models import (
    Cluster,
    ClusterLifecycleState,
    ClusterMachine,
    MachineMembershipState,
)
from app.modules.credentials.models import Credential, CredentialKind
from app.modules.credentials.service import CredentialService
from app.modules.infrastructure import facts as facts_mod
from app.modules.infrastructure.access import connect, run_script, tail
from app.modules.infrastructure.host_access import HostExecutor
from app.modules.infrastructure.models import Machine, MachineLifecycleState
from app.orchestration.context import StepContext
from app.orchestration.workflow import RetryableStepError, StepFailedError

MAX_PARALLEL_NODES = 8
PROVISION_TIMEOUT_SECONDS = 1800
SMOKE_TEST_TIMEOUT_SECONDS = 300


@cache
def _script(name: str) -> str:
    base = resources.files("app.modules.cluster_lifecycle.distributions.kubeadm") / "scripts"
    return (base / "lib.sh").read_text() + "\n" + (base / name).read_text()


def _plan(ctx: StepContext) -> ClusterPlanInput:
    plan: ClusterPlanInput = ctx.params  # type: ignore[assignment]
    endpoint = (ctx.outputs.get("provider-load-balancer") or {}).get("endpoint")
    if endpoint and not plan.spec.control_plane.endpoint:
        spec = plan.spec.model_copy(deep=True)
        spec.control_plane.endpoint = endpoint
        return plan.model_copy(update={"spec": spec})
    return plan


def create_provider_load_balancer(ctx: StepContext) -> dict[str, Any]:
    """Create/reconcile the cloud API endpoint before kubeadm writes its config."""
    plan = _plan(ctx)
    connection_id = plan.spec.provider_connection_id
    if connection_id is None:
        raise StepFailedError("Provider load balancing needs a provider connection", code="provider_connection_missing")
    from app.modules.infrastructure.provider_workflows import provider_for
    from app.modules.providers.types import LoadBalancerRequest

    options = plan.spec.options
    targets = _provider_lb_targets(ctx, plan)
    request = LoadBalancerRequest(
        name=f"kplatform-{plan.cluster_id.hex[:20]}",
        vpc_id=str(options["provider_lb_vpc_id"]),
        subnet_ids=list(options["provider_lb_subnet_ids"]),
        targets=targets,
        scheme=str(options.get("provider_lb_scheme", "internal")),
    )
    with provider_for(ctx, connection_id) as provider:
        result = provider.create_load_balancer(request)
    with ctx.db() as session:
        cluster = session.get(Cluster, plan.cluster_id)
        if cluster is None:
            raise StepFailedError("Cluster was deleted while its load balancer was being created")
        persisted = dict(cluster.spec or {})
        persisted_options = dict(persisted.get("options") or {})
        persisted_options["provider_lb_external_id"] = result.external_id
        persisted["options"] = persisted_options
        control_plane = dict(persisted.get("control_plane") or {})
        control_plane["endpoint"] = result.endpoint
        persisted["control_plane"] = control_plane
        cluster.spec = persisted
    ctx.info(f"Provider API endpoint is {result.endpoint}")
    return {"external_id": result.external_id, "endpoint": result.endpoint}


def _provider_lb_targets(ctx: StepContext, plan: ClusterPlanInput) -> list[str]:
    targets: list[str] = []
    planned_ids = {node.machine_id for node in plan.control_planes}
    with ctx.db() as session:
        memberships = session.scalars(
            select(ClusterMachine).where(
                ClusterMachine.cluster_id == plan.cluster_id,
                ClusterMachine.role == "control_plane",
            )
        )
        for membership in memberships:
            # A removal plan contains currently joined nodes to exclude. Add/create plans contain
            # planned memberships that should enter the target set when they finish joining.
            if membership.machine_id in planned_ids and membership.membership_state is MachineMembershipState.JOINED:
                continue
            if membership.membership_state not in (
                MachineMembershipState.JOINED,
                MachineMembershipState.PLANNED,
                MachineMembershipState.INSTALLING,
            ):
                continue
            if (
                membership.membership_state is not MachineMembershipState.JOINED
                and membership.machine_id not in planned_ids
            ):
                continue
            machine = session.get(Machine, membership.machine_id)
            if machine is None or not machine.external_id:
                raise RetryableStepError(
                    f"Control-plane machine {membership.node_name or membership.machine_id} has no provider ID yet"
                )
            # AWS target groups expect the EC2 instance ID, not the platform's region/id composite.
            targets.append(machine.external_id.rsplit("/", 1)[-1])
    return targets


def sync_provider_load_balancer(ctx: StepContext) -> dict[str, Any]:
    plan = _plan(ctx)
    external_id = plan.spec.options.get("provider_lb_external_id")
    connection_id = plan.spec.provider_connection_id
    if not external_id or connection_id is None:
        raise StepFailedError("The provider load balancer is missing its persisted identifier")
    from app.modules.infrastructure.provider_workflows import provider_for
    from app.modules.providers.types import LoadBalancerRequest

    options = plan.spec.options
    request = LoadBalancerRequest(
        name=f"kplatform-{plan.cluster_id.hex[:20]}",
        vpc_id=str(options["provider_lb_vpc_id"]),
        subnet_ids=list(options["provider_lb_subnet_ids"]),
        targets=_provider_lb_targets(ctx, plan),
        scheme=str(options.get("provider_lb_scheme", "internal")),
    )
    with provider_for(ctx, connection_id) as provider:
        result = provider.create_load_balancer(request)
    if result.external_id != external_id:
        raise StepFailedError("Provider returned a different load balancer for the persisted cluster endpoint")
    return {"external_id": result.external_id, "endpoint": result.endpoint}


def delete_provider_load_balancer(ctx: StepContext) -> dict[str, Any]:
    plan = _plan(ctx)
    external_id = plan.spec.options.get("provider_lb_external_id")
    connection_id = plan.spec.provider_connection_id
    if not external_id:
        return {"deleted": False, "reason": "not provisioned"}
    if connection_id is None:
        raise StepFailedError("Cannot delete the provider load balancer without its provider connection")
    from app.modules.infrastructure.provider_workflows import provider_for

    with provider_for(ctx, connection_id) as provider:
        provider.delete_load_balancer(str(external_id))
    return {"deleted": True}


def _options(plan: ClusterPlanInput) -> cfg.KubeadmOptions:
    return cfg.KubeadmOptions.model_validate(plan.spec.options)


def _ip(ctx: StepContext, node: PlannedNode) -> str:
    return ctx.outputs["validate"]["nodes"][str(node.machine_id)]["ip"]


def _iface(ctx: StepContext, node: PlannedNode) -> str:
    return ctx.outputs["validate"]["nodes"][str(node.machine_id)].get("interface") or "eth0"


def _interface_for(executor: HostExecutor, ip: str | None) -> tuple[str | None, str | None]:
    """(interface, address/prefix) carrying ``ip``, from `ip -o -4 addr show`."""
    from app.modules.infrastructure.host_access import CommandOptions

    result = executor.run("ip -o -4 addr show", CommandOptions(timeout_seconds=30))
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 4 and fields[2] == "inet" and fields[3].split("/")[0] == ip:
            return fields[1].split("@")[0], fields[3]
    return None, None


def _kube_vip(ctx: StepContext, node: PlannedNode, executor: HostExecutor, vip: str, *, bootstrapping: bool) -> None:
    manifest = cfg.render_kube_vip_manifest(
        vip=vip,
        interface=_iface(ctx, node),
        kubeconfig=cfg.kube_vip_kubeconfig(_version(ctx), bootstrapping=bootstrapping),
    )
    _execute(
        ctx,
        node,
        executor,
        "kube_vip.sh",
        {
            "MANIFEST_B64": base64.b64encode(manifest.encode()).decode(),
            # Before kubeadm init there is no API server to wait for.
            "VIP": "" if bootstrapping else vip,
        },
        action="kube-vip install",
        timeout=420,
    )


def _version(ctx: StepContext) -> str:
    return ctx.outputs["resolve-version"]["version"]


def _execute(
    ctx: StepContext,
    node: PlannedNode,
    executor: HostExecutor,
    script: str,
    env: dict[str, str],
    *,
    action: str,
    timeout: float = 1800,
    stream: bool = True,
) -> str:
    def on_output(line: str) -> None:
        ctx.info(f"[{node.name}] {line}")

    result = run_script(
        executor, _script(script), env=env, timeout_seconds=timeout, on_output=on_output if stream else None
    )
    if not result.ok:
        raise StepFailedError(
            f"{action} failed on {node.name} (exit {result.exit_code}):\n{tail(result)}",
            code="node_command_failed",
        )
    return result.stdout


def _script_action(
    ctx: StepContext, script: str, env: dict[str, str], *, action: str, timeout: float = 1800
) -> Callable[[PlannedNode, HostExecutor], dict[str, Any] | None]:
    def run(node: PlannedNode, executor: HostExecutor) -> dict[str, Any] | None:
        _execute(ctx, node, executor, script, env, action=action, timeout=timeout)
        return None

    return run


def _for_nodes(
    ctx: StepContext,
    nodes: list[PlannedNode],
    action: Callable[[PlannedNode, HostExecutor], dict[str, Any] | None],
    *,
    parallel: bool = True,
    tolerate_unreachable: bool = False,
) -> dict[str, Any]:
    """Run ``action`` on each node (in parallel by default), aggregating failures."""

    def run_one(node: PlannedNode) -> dict[str, Any] | None:
        ctx.check_cancelled()
        with connect(ctx, node.machine_id) as executor:
            return action(node, executor)

    results: dict[str, Any] = {}
    errors: dict[str, BaseException] = {}
    if parallel and len(nodes) > 1:
        with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL_NODES, len(nodes))) as pool:
            futures = {node.name: (node, pool.submit(run_one, node)) for node in nodes}
            for name, (node, future) in futures.items():
                try:
                    results[str(node.machine_id)] = future.result() or {}
                except BaseException as exc:
                    errors[name] = exc
    else:
        for node in nodes:
            try:
                results[str(node.machine_id)] = run_one(node) or {}
            except BaseException as exc:
                errors[node.name] = exc
                if not tolerate_unreachable:
                    break

    if errors:
        from app.orchestration.workflow import OperationCancelledError

        if any(isinstance(e, OperationCancelledError) for e in errors.values()):
            raise OperationCancelledError()
        if tolerate_unreachable and all(isinstance(e, RetryableStepError) for e in errors.values()):
            for name, error in errors.items():
                ctx.warning(f"[{name}] skipped: {error}")
            return results
        summary = "; ".join(f"{name}: {error}" for name, error in errors.items())
        if all(isinstance(e, RetryableStepError) for e in errors.values()):
            raise RetryableStepError(summary)
        raise StepFailedError(summary, code="node_failures")
    return results


# --------------------------------------------------------------------------- create


def validate(ctx: StepContext) -> dict[str, Any]:
    plan = _plan(ctx)
    problems: list[str] = []

    def check(node: PlannedNode, executor: HostExecutor) -> dict[str, Any]:
        result = run_script(executor, facts_mod.detect_script(), sudo=False, timeout_seconds=120)
        facts = facts_mod.parse_facts(result.stdout)
        sudo_ok = executor.run("true", _sudo_opts()).ok
        failures = [c for c in facts_mod.evaluate(facts, sudo_ok=sudo_ok) if c.status is facts_mod.CheckStatus.FAIL]
        for c in failures:
            problems.append(f"{node.name}: {c.label}: {c.message}")
        if node.role == "control_plane" and int(facts.get("cpu_cores") or 0) < facts_mod.MIN_CPUS_CONTROL_PLANE:
            problems.append(f"{node.name}: control-plane nodes need at least 2 CPUs")
        if facts.get("has_kubelet_conf") == "yes":
            problems.append(
                f"{node.name}: already belongs to a Kubernetes cluster "
                "(/etc/kubernetes/kubelet.conf exists); reset it before using it for a new cluster"
            )
        interface, cidr = _interface_for(executor, facts.get("primary_ip"))
        ctx.info(
            f"[{node.name}] {facts.get('os_pretty_name')}, {facts.get('cpu_cores')} CPU, "
            f"{facts.get('memory_mb')} MB, address {facts.get('primary_ip')} ({interface or 'interface unknown'})"
        )
        return {
            "ip": facts.get("primary_ip"),
            "os": facts.get("os_pretty_name"),
            "hostname": facts.get("hostname"),
            "interface": interface,
            "cidr": cidr,
        }

    nodes = _for_nodes(ctx, plan.nodes, check)
    ips = [n["ip"] for n in nodes.values()]
    if len(set(ips)) != len(ips):
        problems.append("two or more machines report the same primary IP address")
    vip = cfg.vip_of(plan.spec)
    if vip:
        for node in plan.control_planes:
            info = nodes.get(str(node.machine_id), {})
            if vip == info.get("ip"):
                problems.append(f"{node.name}: the kube-vip address {vip} is this machine's own address")
            elif not info.get("cidr") or ipaddress.ip_address(vip) not in ipaddress.ip_interface(info["cidr"]).network:
                problems.append(
                    f"{node.name}: the kube-vip address {vip} is not in this machine's network "
                    f"({info.get('cidr') or 'unknown'}); kube-vip (ARP) needs an unused address on the same subnet"
                )
    if problems:
        raise StepFailedError("Machines are not ready for Kubernetes:\n- " + "\n- ".join(problems), code="preflight")
    return {"nodes": nodes}


def _sudo_opts() -> Any:
    from app.modules.infrastructure.host_access import CommandOptions

    return CommandOptions(sudo=True, timeout_seconds=30)


def prepare(ctx: StepContext) -> dict[str, Any]:
    plan = _plan(ctx)
    options = _options(plan)

    def run(node: PlannedNode, executor: HostExecutor) -> None:
        _execute(
            ctx,
            node,
            executor,
            "prepare.sh",
            {
                "DISABLE_SWAP": "0" if options.allow_swap else "1",
                "NODE_ROLE": node.role,
                "CNI": plan.spec.network.cni.value,
            },
            action="OS preparation",
        )

    _for_nodes(ctx, plan.nodes, run)
    return {}


def install_containerd(ctx: StepContext) -> dict[str, Any]:
    plan = _plan(ctx)
    _for_nodes(
        ctx,
        plan.nodes,
        _script_action(ctx, "containerd.sh", {}, action="containerd install"),
    )
    return {}


def resolve_version(ctx: StepContext) -> dict[str, Any]:
    plan = _plan(ctx)
    requested = plan.spec.kubernetes_version.lstrip("v")
    _, _, patch = cfg.parse_version(requested)
    first = plan.control_planes[0]
    resolved: dict[str, str] = {}

    def run(node: PlannedNode, executor: HostExecutor) -> None:
        out = _execute(
            ctx,
            node,
            executor,
            "packages.sh",
            {
                "K8S_MINOR": cfg.minor_of(requested),
                "K8S_VERSION": requested if patch is not None else "",
                "MODE": "resolve",
            },
            action="Version resolution",
            stream=False,
        )
        for line in out.splitlines():
            if line.startswith("RESOLVED_VERSION="):
                resolved["version"] = line.split("=", 1)[1].strip()

    _for_nodes(ctx, [first], run, parallel=False)
    if not resolved.get("version"):
        raise StepFailedError("Could not determine the Kubernetes version to install", code="version_unresolved")
    ctx.info(f"Kubernetes {requested} resolves to v{resolved['version']}")
    return resolved


def install_packages(ctx: StepContext) -> dict[str, Any]:
    plan = _plan(ctx)
    version = _version(ctx)
    env = {"K8S_MINOR": cfg.minor_of(version), "K8S_VERSION": version, "MODE": "install"}
    _for_nodes(
        ctx,
        plan.nodes,
        _script_action(ctx, "packages.sh", env, action="Kubernetes package install"),
    )
    return {}


def upgrade_kubeadm(node: PlannedNode, target_version: str, *, apply: bool) -> Callable[[StepContext], dict[str, Any]]:
    """Upgrade one node's kubeadm binary and run the role-appropriate kubeadm upgrade command."""

    def run(ctx: StepContext) -> dict[str, Any]:
        ctx.check_cancelled()
        with connect(ctx, node.machine_id) as executor:
            _execute(
                ctx,
                node,
                executor,
                "packages.sh",
                {
                    "K8S_MINOR": cfg.minor_of(target_version),
                    "K8S_VERSION": target_version,
                    "MODE": "upgrade-kubeadm",
                },
                action="kubeadm package upgrade",
                timeout=1200,
            )
            _execute(
                ctx,
                node,
                executor,
                "upgrade.sh",
                {"TARGET_VERSION": target_version, "APPLY_UPGRADE": "1" if apply else "0"},
                action="kubeadm upgrade",
                timeout=1800,
            )
        return {"node": node.name, "version": target_version, "control_plane_apply": apply}

    return run


def upgrade_node(node: PlannedNode, target_version: str) -> Callable[[StepContext], dict[str, Any]]:
    """Drain a node, upgrade/restart kubelet, wait for Ready, then uncordon it."""

    def run(ctx: StepContext) -> dict[str, Any]:
        plan = _plan(ctx)
        coordinator = next(
            (n for n in plan.control_planes if n.machine_id != node.machine_id),
            node,
        )
        from app.modules.infrastructure.host_access import CommandOptions

        emptydir_flag = " --delete-emptydir-data" if plan.allow_emptydir_data else ""
        if plan.allow_emptydir_data:
            ctx.warning(f"[{node.name}] drain may delete ephemeral emptyDir data, as explicitly requested")
        with connect(ctx, coordinator.machine_id) as api_host:
            _kubectl(
                api_host,
                f"drain {shlex.quote(node.name)} --ignore-daemonsets{emptydir_flag} --timeout=15m",
                timeout=960,
            )
            ctx.info(f"[{node.name}] drained; PodDisruptionBudgets were respected")
            with connect(ctx, node.machine_id) as executor:
                _execute(
                    ctx,
                    node,
                    executor,
                    "packages.sh",
                    {
                        "K8S_MINOR": cfg.minor_of(target_version),
                        "K8S_VERSION": target_version,
                        "MODE": "upgrade-kubelet",
                    },
                    action="kubelet and kubectl package upgrade",
                    timeout=1200,
                )
                restart = executor.run("systemctl restart kubelet", CommandOptions(sudo=True, timeout_seconds=120))
                if not restart.ok:
                    raise StepFailedError(f"Could not restart kubelet on {node.name}", code="kubelet_restart_failed")
            deadline = time.monotonic() + 900
            last_problem = "node has not reported the target kubelet version"
            while time.monotonic() < deadline:
                ctx.check_cancelled()
                try:
                    observed = json.loads(_kubectl(api_host, f"get node {shlex.quote(node.name)} -o json"))
                    status = observed.get("status", {})
                    version = (status.get("nodeInfo") or {}).get("kubeletVersion")
                    ready = any(
                        condition.get("type") == "Ready" and condition.get("status") == "True"
                        for condition in status.get("conditions", [])
                    )
                    if version == f"v{target_version}" and ready:
                        break
                    last_problem = f"kubelet={version or 'unknown'}, Ready={ready}"
                except StepFailedError as exc:
                    last_problem = str(exc)
                time.sleep(5)
            else:
                raise StepFailedError(
                    f"{node.name} did not become Ready at kubelet v{target_version} within 15 minutes: {last_problem}",
                    code="kubelet_not_ready",
                )
            _kubectl(api_host, f"uncordon {shlex.quote(node.name)}")
        ctx.info(f"[{node.name}] running kubelet {target_version}; node is Ready and schedulable")
        return {"node": node.name, "kubelet_version": target_version, "ready": True}

    return run


def verify_upgrade(ctx: StepContext) -> dict[str, Any]:
    plan = _plan(ctx)
    target = plan.target_version
    if not target:
        raise StepFailedError("Upgrade target version is missing", code="version_unknown")
    with connect(ctx, plan.control_planes[0].machine_id) as executor:
        raw = _kubectl(executor, "get nodes -o json")
        server_version = json.loads(_kubectl(executor, "version -o json")).get("serverVersion", {}).get("gitVersion")
    nodes = json.loads(raw).get("items", [])
    expected = f"v{target}"
    observed = {
        item.get("metadata", {}).get("name"): item.get("status", {}).get("nodeInfo", {}).get("kubeletVersion")
        for item in nodes
    }
    problems = [
        f"{name}: expected {expected}, found {version or 'unknown'}"
        for name, version in observed.items()
        if version != expected
    ]
    not_ready = [
        item.get("metadata", {}).get("name")
        for item in nodes
        if not any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in item.get("status", {}).get("conditions", [])
        )
    ]
    if problems or not_ready or len(observed) < len(plan.nodes):
        details = problems + ([f"Not Ready: {', '.join(not_ready)}"] if not_ready else [])
        if len(observed) < len(plan.nodes):
            details.append(f"Expected {len(plan.nodes)} nodes, found {len(observed)}")
        raise StepFailedError("Upgrade verification failed: " + "; ".join(details), code="upgrade_verification_failed")
    if server_version != expected:
        raise StepFailedError(
            f"Upgrade verification failed: API server reports {server_version or 'unknown'}, expected {expected}",
            code="upgrade_verification_failed",
        )
    ctx.info(f"Verified {len(observed)} Ready node(s) at Kubernetes {expected}")
    ctx.set_result({"cluster": plan.cluster_name, "kubernetes_version": expected, "nodes": len(observed)})
    return {"kubernetes_version": expected, "nodes": len(observed)}


def init_control_plane(ctx: StepContext) -> dict[str, Any]:
    plan = _plan(ctx)
    options = _options(plan)
    version = _version(ctx)
    first = plan.control_planes[0]
    ha = len(plan.control_planes) > 1
    token = cfg.generate_bootstrap_token()
    certificate_key = cfg.generate_certificate_key() if ha else None
    ctx.redact(token, *([certificate_key] if certificate_key else []))
    document = cfg.render_init_config(
        spec=plan.spec,
        options=options,
        version=version,
        node_name=first.name,
        advertise_address=_ip(ctx, first),
        bootstrap_token=token,
        certificate_key=certificate_key,
    )
    output: dict[str, str] = {}

    vip = cfg.vip_of(plan.spec)

    def run(node: PlannedNode, executor: HostExecutor) -> None:
        if vip:
            # kube-vip must hold the address before kubeadm waits for the API at the endpoint.
            _kube_vip(ctx, node, executor, vip, bootstrapping=True)
        out = _execute(
            ctx,
            node,
            executor,
            "init.sh",
            {"CONFIG_B64": base64.b64encode(document.encode()).decode(), "UPLOAD_CERTS": "1" if ha else "0"},
            action="kubeadm init",
        )
        for line in out.splitlines():
            if line.startswith("CA_CERT_HASH="):
                output["ca_cert_hash"] = line.split("=", 1)[1].strip()
        if vip:
            _kube_vip(ctx, node, executor, vip, bootstrapping=False)
            ctx.info(f"[{node.name}] kube-vip announces {vip}")

    _for_nodes(ctx, [first], run, parallel=False)
    if "ca_cert_hash" not in output:
        raise StepFailedError("kubeadm init did not report the CA certificate hash", code="init_incomplete")
    output["api_server_endpoint"] = cfg.control_plane_endpoint(plan.spec) or f"{_ip(ctx, first)}:6443"
    return output


def install_cni(ctx: StepContext) -> dict[str, Any]:
    plan = _plan(ctx)
    cni = plan.spec.network.cni.value
    version, url = cfg.CNI_MANIFESTS[cni]
    env = {"CNI": cni, "MANIFEST_URL": url.format(version=version), "POD_CIDR": plan.spec.network.pod_cidr}
    _for_nodes(
        ctx,
        [plan.control_planes[0]],
        _script_action(ctx, "cni.sh", env, action=f"{cni} install", timeout=900),
        parallel=False,
    )
    return {"cni": cni, "version": version}


def _kubectl(executor: HostExecutor, args: str, *, timeout: float = 120) -> str:
    from app.modules.infrastructure.host_access import CommandOptions

    result = executor.run(
        f"kubectl --kubeconfig /etc/kubernetes/admin.conf {args}",
        CommandOptions(sudo=True, timeout_seconds=timeout),
    )
    if not result.ok:
        verb = " ".join(w for w in args.split()[:4] if not w.startswith("-"))
        raise StepFailedError(f"kubectl {verb} failed: {result.stderr.strip()[-400:]}", code="kubectl_failed")
    return result.stdout


def _join_material(ctx: StepContext, executor: HostExecutor, *, control_plane: bool) -> tuple[str, str | None]:
    """Create a fresh bootstrap token (plus a certificate key for control-plane joins) on the first
    control plane."""
    from app.modules.infrastructure.host_access import CommandOptions

    token = cfg.generate_bootstrap_token()
    certificate_key = cfg.generate_certificate_key() if control_plane else None
    ctx.redact(token, *([certificate_key] if certificate_key else []))
    result = executor.run(f"kubeadm token create {token} --ttl 2h", CommandOptions(sudo=True, timeout_seconds=120))
    if not result.ok:
        raise StepFailedError(f"Could not create a join token: {result.stderr.strip()}", code="token_failed")
    if certificate_key:
        result = executor.run(
            f"kubeadm init phase upload-certs --upload-certs --certificate-key {certificate_key}",
            CommandOptions(sudo=True, timeout_seconds=180),
        )
        if not result.ok:
            raise StepFailedError(
                f"Could not upload control-plane certificates: {result.stderr.strip()}",
                code="upload_certs_failed",
            )
    return token, certificate_key


def _join(ctx: StepContext, nodes: list[PlannedNode], *, control_plane: bool) -> dict[str, Any]:
    plan = _plan(ctx)
    options = _options(plan)
    version = _version(ctx)
    init = ctx.outputs["init"]
    vip = cfg.vip_of(plan.spec) if control_plane else None
    with connect(ctx, plan.lead.machine_id) as cp:
        token, certificate_key = _join_material(ctx, cp, control_plane=control_plane)

    def run(node: PlannedNode, executor: HostExecutor) -> None:
        document = cfg.render_join_config(
            version=version,
            options=options,
            node_name=node.name,
            advertise_address=_ip(ctx, node),
            api_server_endpoint=init["api_server_endpoint"],
            token=token,
            ca_cert_hash=init["ca_cert_hash"],
            certificate_key=certificate_key,
        )
        _execute(
            ctx,
            node,
            executor,
            "join.sh",
            {"CONFIG_B64": base64.b64encode(document.encode()).decode()},
            action="kubeadm join",
        )
        if vip:
            # After the join: kubeadm's preflight requires an empty manifests directory.
            _kube_vip(ctx, node, executor, vip, bootstrapping=False)

    # Control planes join one at a time (etcd membership changes must be serialized).
    _for_nodes(ctx, nodes, run, parallel=not control_plane)
    return {}


def join_control_plane(node: PlannedNode) -> Callable[[StepContext], dict[str, Any]]:
    return lambda ctx: _join(ctx, [node], control_plane=True)


def join_workers(ctx: StepContext) -> dict[str, Any]:
    return _join(ctx, _plan(ctx).workers, control_plane=False)


def configure_nodes(ctx: StepContext) -> dict[str, Any]:
    plan = _plan(ctx)
    options = _options(plan)
    pools = {
        plan.spec.control_plane.name: plan.spec.control_plane,
        **{p.name: p for p in plan.spec.worker_pools},
    }
    allow_cp_workloads = options.control_plane_workloads

    with connect(ctx, plan.lead.machine_id) as executor:
        if allow_cp_workloads is None:
            if plan.anchor is not None:
                # Adding nodes: keep the cluster's current policy (mirror the anchor's taint).
                anchor = json.loads(_kubectl(executor, f"get node {shlex.quote(plan.anchor.name)} -o json"))
                taints = (anchor.get("spec") or {}).get("taints") or []
                allow_cp_workloads = not any(t.get("key") == "node-role.kubernetes.io/control-plane" for t in taints)
            else:
                allow_cp_workloads = not plan.workers
        for node in plan.nodes:
            pool = pools[node.pool]
            labels = {"kplatform.io/pool": node.pool, **pool.labels}
            if node.role == "worker":
                labels["node-role.kubernetes.io/worker"] = ""
            label_args = " ".join(shlex.quote(f"{k}={v}") for k, v in labels.items())
            _kubectl(executor, f"label node {shlex.quote(node.name)} --overwrite {label_args}")
            for taint in pool.taints:
                spec = f"{taint.key}={taint.value or ''}:{taint.effect.value}"
                _kubectl(executor, f"taint node {shlex.quote(node.name)} --overwrite {shlex.quote(spec)}")
            if node.role == "control_plane" and allow_cp_workloads:
                from app.modules.infrastructure.host_access import CommandOptions

                executor.run(
                    "kubectl --kubeconfig /etc/kubernetes/admin.conf taint node "
                    f"{shlex.quote(node.name)} node-role.kubernetes.io/control-plane:NoSchedule-",
                    CommandOptions(sudo=True, timeout_seconds=60),
                )
            ctx.info(f"[{node.name}] labels applied ({len(labels)}), taints applied ({len(pool.taints)})")
    if allow_cp_workloads:
        ctx.info("Control-plane nodes accept regular workloads")
    return {}


def wait_ready(ctx: StepContext) -> dict[str, Any]:
    plan = _plan(ctx)
    options = _options(plan)
    expected = {n.name for n in plan.nodes}
    deadline = time.monotonic() + options.ready_timeout_seconds
    last_report = ""
    with connect(ctx, plan.lead.machine_id) as executor:
        while True:
            ctx.check_cancelled()
            nodes = json.loads(_kubectl(executor, "get nodes -o json")).get("items", [])
            ready = {
                n["metadata"]["name"]
                for n in nodes
                if any(c["type"] == "Ready" and c["status"] == "True" for c in n["status"].get("conditions", []))
            }
            report = f"{len(ready & expected)}/{len(expected)} nodes Ready"
            if report != last_report:
                ctx.info(report + (f" (waiting for {', '.join(sorted(expected - ready))})" if expected - ready else ""))
                last_report = report
            if expected <= ready:
                break
            if time.monotonic() > deadline:
                raise StepFailedError(
                    f"Nodes did not become Ready within {options.ready_timeout_seconds}s: "
                    f"{', '.join(sorted(expected - ready))}",
                    code="nodes_not_ready",
                )
            ctx.set_progress(min(0.9, len(ready & expected) / len(expected)))
            time.sleep(5)
        # Poll availability rather than `rollout status`: if pods were blocked for a long time
        # (e.g. before the CNI came up) the Deployment reports "progress deadline exceeded"
        # even after its pods become healthy.
        ctx.info("Waiting for cluster DNS (CoreDNS)")
        while True:
            ctx.check_cancelled()
            status = json.loads(_kubectl(executor, "-n kube-system get deployment coredns -o json")).get("status", {})
            wanted = status.get("replicas") or 0
            if wanted and (status.get("availableReplicas") or 0) >= wanted:
                ctx.info(f"CoreDNS available ({wanted}/{wanted})")
                break
            if time.monotonic() > deadline:
                raise StepFailedError(
                    f"CoreDNS did not become available ({status.get('availableReplicas') or 0}/{wanted})",
                    code="dns_not_ready",
                )
            time.sleep(5)
        summary = [
            {
                "name": n["metadata"]["name"],
                "kubelet": n["status"]["nodeInfo"]["kubeletVersion"],
                "os": n["status"]["nodeInfo"]["osImage"],
                "runtime": n["status"]["nodeInfo"]["containerRuntimeVersion"],
            }
            for n in nodes
        ]
    ctx.info("All nodes are Ready and cluster DNS is running")
    return {"nodes": summary}


def register(ctx: StepContext) -> dict[str, Any]:
    plan = _plan(ctx)
    with connect(ctx, plan.control_planes[0].machine_id) as executor:
        kubeconfig = executor.get_file("/etc/kubernetes/admin.conf", sudo=True).decode()
    for line in kubeconfig.splitlines():
        if "client-key-data" in line or "token:" in line:
            ctx.redact(line.split(":", 1)[1].strip())

    principal = Principal.system(ctx.organization_id, "cluster-installer")
    credential_name = f"cluster-{plan.cluster_name}-admin"
    with ctx.db() as session:
        service = CredentialService(session, ctx.secret_store)
        existing = session.scalar(
            select(Credential).where(
                Credential.organization_id == ctx.organization_id, Credential.name == credential_name
            )
        )
        if existing is None:
            credential = service.create(
                principal,
                name=credential_name,
                kind=CredentialKind.KUBECONFIG,
                secret_value=kubeconfig,
                description=f"Admin kubeconfig for cluster {plan.cluster_name} (created by the installer)",
                public_metadata={"cluster": plan.cluster_name},
            )
        else:
            credential = service.replace_secret(principal, existing.id, kubeconfig)
        cluster = session.get(Cluster, plan.cluster_id)
        if cluster is None:
            raise StepFailedError("Cluster record was deleted during installation", code="cluster_missing")
        cluster.access_credential_id = credential.id
        cluster.api_endpoint = f"https://{ctx.outputs['init']['api_server_endpoint']}"
        # Machines whose API port is only reachable through a published endpoint (local
        # container nodes) get a connection override; the certificate is still verified
        # against the node's own address.
        first_cp = session.get(Machine, plan.control_planes[0].machine_id)
        published = (first_cp.provider_metadata or {}) if first_cp else {}
        external_lb = cfg.control_plane_endpoint(plan.spec) and not cfg.vip_of(plan.spec)
        if not external_lb and published.get("kube_api_host") and not cluster.api_server_override:
            cluster.api_server_override = f"https://{published['kube_api_host']}:{published['kube_api_port']}"
            cluster.tls_server_name = _ip(ctx, plan.control_planes[0])
            ctx.info(
                f"Kubernetes API reachable through {cluster.api_server_override} (TLS name {cluster.tls_server_name})"
            )
        cluster.kubernetes_version = f"v{_version(ctx)}"
        cluster.lifecycle_state = ClusterLifecycleState.ACTIVE
        cluster.last_contact_at = datetime.now(UTC)
        cluster.status_detail = {
            "nodes": ctx.outputs.get("wait-ready", {}).get("nodes", []),
            "cni": ctx.outputs.get("cni", {}),
        }
        for membership in session.scalars(select(ClusterMachine).where(ClusterMachine.cluster_id == plan.cluster_id)):
            membership.membership_state = MachineMembershipState.JOINED
        session.commit()

    ctx.info(f"Administrator kubeconfig stored as encrypted credential '{credential_name}'")
    ctx.set_result(
        {
            "cluster": plan.cluster_name,
            "kubernetes_version": f"v{_version(ctx)}",
            "api_endpoint": f"https://{ctx.outputs['init']['api_server_endpoint']}",
            "nodes": len(plan.nodes),
            "kubeconfig_credential": credential_name,
        }
    )
    return {"credential": credential_name}


# --------------------------------------------------------------------------- delete


def reset_nodes(ctx: StepContext) -> dict[str, Any]:
    plan = _plan(ctx)
    if not plan.nodes:
        return {}
    _for_nodes(
        ctx,
        plan.nodes,
        _script_action(ctx, "reset.sh", {"VIP": cfg.vip_of(plan.spec) or ""}, action="kubeadm reset", timeout=600),
        tolerate_unreachable=plan.force,
    )
    return {}


def release(ctx: StepContext) -> dict[str, Any]:
    plan = _plan(ctx)
    with ctx.db() as session:
        cluster = session.get(Cluster, plan.cluster_id)
        if cluster is None:
            return {}  # already released (resumed step)
        credential_id = cluster.access_credential_id
        session.execute(delete(ClusterMachine).where(ClusterMachine.cluster_id == plan.cluster_id))
        cluster.access_credential_id = None
        session.flush()
        if credential_id:
            credential = session.get(Credential, credential_id)
            if credential is not None:
                session.delete(credential)
        session.delete(cluster)
    ctx.info(f"Cluster {plan.cluster_name} removed; {len(plan.nodes)} machine(s) released back to the inventory")
    ctx.set_result({"cluster": plan.cluster_name, "released_machines": [n.name for n in plan.nodes]})
    return {}


# --------------------------------------------------------------------------- shared: provisioning


def wait_machines(ctx: StepContext) -> dict[str, Any]:
    """Wait until machines the platform is provisioning for this plan are ready."""
    plan = _plan(ctx)
    pending = [n for n in plan.nodes if n.provisioned]
    deadline = time.monotonic() + PROVISION_TIMEOUT_SECONDS
    last = ""
    while True:
        ctx.check_cancelled()
        with ctx.db() as session:
            states = {}
            for node in pending:
                machine = session.get(Machine, node.machine_id)
                if machine is None:
                    raise StepFailedError(f"Machine {node.name} was removed during provisioning", code="machine_gone")
                states[node.name] = (machine.lifecycle_state, machine.last_error)
        failed = {
            n: e
            for n, (st, e) in states.items()
            if st in (MachineLifecycleState.ERROR, MachineLifecycleState.UNREACHABLE)
        }
        if failed:
            raise StepFailedError(
                "Machine provisioning failed:\n- "
                + "\n- ".join(f"{n}: {e or 'see its machine.provision operation'}" for n, e in failed.items())
                + "\nFix the cause, retry the machine.provision operation(s), then retry this step.",
                code="provisioning_failed",
            )
        ready = sorted(n for n, (st, _) in states.items() if st is MachineLifecycleState.READY)
        report = f"{len(ready)}/{len(pending)} machine(s) ready"
        if report != last:
            ctx.info(report)
            last = report
        if len(ready) == len(pending):
            return {"machines": ready}
        if time.monotonic() > deadline:
            raise StepFailedError(
                f"Machines were not ready within {PROVISION_TIMEOUT_SECONDS}s", code="provision_timeout"
            )
        ctx.set_progress(len(ready) / max(1, len(pending)))
        time.sleep(5)


def _destroy(ctx: StepContext, nodes: list[PlannedNode]) -> list[str]:
    """Delete platform-provisioned machines at their provider and drop them from the inventory."""
    from app.modules.infrastructure.provider_workflows import PENDING_PREFIX, provider_for
    from app.modules.providers.types import MachineRef

    destroyed: list[str] = []
    for node in nodes:
        with ctx.db() as session:
            machine = session.get(Machine, node.machine_id)
            if machine is None:
                continue  # already gone (resumed step)
            if not machine.provisioned:
                ctx.info(f"[{node.name}] registered server, not provisioned by the platform: kept")
                continue
            connection_id, external_id = machine.provider_connection_id, machine.external_id
        with provider_for(ctx, connection_id) as provider:
            if not external_id.startswith(PENDING_PREFIX):
                provider.delete(MachineRef(external_id=external_id))
        with ctx.db() as session:
            machine = session.get(Machine, node.machine_id)
            if machine is not None:
                session.delete(machine)
        ctx.info(f"[{node.name}] machine destroyed")
        destroyed.append(node.name)
    return destroyed


def destroy_machines(ctx: StepContext) -> dict[str, Any]:
    plan = _plan(ctx)
    destroyed = _destroy(ctx, plan.nodes)
    ctx.set_result({**(ctx.outputs.get("release") or {}), "destroyed_machines": destroyed})
    return {"destroyed": destroyed}


# --------------------------------------------------------------------------- create: smoke test


def smoke_test(ctx: StepContext) -> dict[str, Any]:
    """Schedule a pod, pull an image and resolve the API service through cluster DNS."""
    plan = _plan(ctx)
    name = f"kplatform-smoke-{secrets.token_hex(3)}"
    domain = plan.spec.network.dns_domain
    service_ip = str(next(ipaddress.ip_network(plan.spec.network.service_cidr).hosts()))
    overrides = {"spec": {"tolerations": [{"operator": "Exists"}], "restartPolicy": "Never"}}
    with connect(ctx, plan.lead.machine_id) as executor:
        _kubectl(
            executor,
            f"-n kube-system run {name} --image={cfg.SMOKE_TEST_IMAGE} --restart=Never "
            f"--labels=app.kubernetes.io/managed-by=kubernetes-platform "
            f"--overrides={shlex.quote(json.dumps(overrides))} "
            f"--command -- nslookup kubernetes.default.svc.{domain}",
        )
        try:
            deadline = time.monotonic() + SMOKE_TEST_TIMEOUT_SECONDS
            while True:
                ctx.check_cancelled()
                pod = json.loads(_kubectl(executor, f"-n kube-system get pod {name} -o json"))
                phase = (pod.get("status") or {}).get("phase")
                if phase in ("Succeeded", "Failed"):
                    break
                if time.monotonic() > deadline:
                    waiting = [
                        (c.get("state") or {}).get("waiting", {}).get("reason")
                        for c in (pod.get("status") or {}).get("containerStatuses") or []
                    ]
                    raise StepFailedError(
                        f"Smoke-test pod did not finish within {SMOKE_TEST_TIMEOUT_SECONDS}s "
                        f"(phase {phase}, {', '.join(w for w in waiting if w) or 'no container status'})",
                        code="smoke_test_timeout",
                    )
                time.sleep(3)
            node = (pod.get("spec") or {}).get("nodeName")
            logs = _kubectl(executor, f"-n kube-system logs {name}")
        finally:
            from app.modules.infrastructure.host_access import CommandOptions

            executor.run(
                f"kubectl --kubeconfig /etc/kubernetes/admin.conf -n kube-system delete pod {name} --wait=false",
                CommandOptions(sudo=True, timeout_seconds=60),
            )
    if phase != "Succeeded" or service_ip not in logs:
        raise StepFailedError(
            f"Cluster DNS check failed on {node}: kubernetes.default.svc.{domain} did not resolve to {service_ip}.\n"
            + logs[-800:],
            code="smoke_test_failed",
        )
    ctx.info(f"Pod scheduled on {node}, image pulled, kubernetes.default.svc.{domain} resolved to {service_ip}")
    return {"node": node}


# --------------------------------------------------------------------------- add nodes


def cluster_version(ctx: StepContext) -> dict[str, Any]:
    """The version the cluster runs (new nodes must match it)."""
    plan = _plan(ctx)
    with ctx.db() as session:
        cluster = session.get(Cluster, plan.cluster_id)
        version = (cluster.kubernetes_version or "").lstrip("v") if cluster else ""
    if not version:
        raise StepFailedError("The cluster's Kubernetes version is unknown", code="version_unknown")
    ctx.info(f"New nodes get Kubernetes v{version}, matching the cluster")
    return {"version": version}


def join_info(ctx: StepContext) -> dict[str, Any]:
    """API endpoint and CA hash of the existing cluster (what `init` reports on creation)."""
    plan = _plan(ctx)
    from app.modules.infrastructure.host_access import CommandOptions

    with ctx.db() as session:
        cluster = session.get(Cluster, plan.cluster_id)
        endpoint = (cluster.api_endpoint or "").removeprefix("https://") if cluster else ""
    if not endpoint:
        raise StepFailedError("The cluster has no recorded API endpoint", code="endpoint_unknown")
    with connect(ctx, plan.lead.machine_id) as executor:
        result = executor.run(
            "openssl x509 -pubkey -in /etc/kubernetes/pki/ca.crt | openssl pkey -pubin -outform der 2>/dev/null"
            " | openssl dgst -sha256 -hex | sed 's/^.* //'",
            CommandOptions(sudo=True, timeout_seconds=60),
        )
    digest = result.stdout.strip()
    if not result.ok or len(digest) != 64:
        raise StepFailedError(f"Could not read the cluster CA from {plan.lead.name}", code="ca_unreadable")
    ctx.info(f"Joining through {endpoint}")
    return {"api_server_endpoint": endpoint, "ca_cert_hash": f"sha256:{digest}"}


def finalize_add(ctx: StepContext) -> dict[str, Any]:
    plan = _plan(ctx)
    with ctx.db() as session:
        cluster = session.get(Cluster, plan.cluster_id)
        if cluster is None:
            raise StepFailedError("Cluster record was deleted", code="cluster_missing")
        cluster.spec = plan.spec.model_dump(mode="json")
        ids = {n.machine_id for n in plan.nodes}
        for membership in session.scalars(select(ClusterMachine).where(ClusterMachine.cluster_id == plan.cluster_id)):
            if membership.machine_id in ids:
                membership.membership_state = MachineMembershipState.JOINED
    names = [n.name for n in plan.nodes]
    ctx.info(f"{len(names)} node(s) added: {', '.join(names)}")
    ctx.set_result({"cluster": plan.cluster_name, "added_nodes": names})
    return {"added": names}


# --------------------------------------------------------------------------- remove nodes


def drain_nodes(ctx: StepContext) -> dict[str, Any]:
    plan = _plan(ctx)
    from app.modules.infrastructure.host_access import CommandOptions

    with connect(ctx, plan.lead.machine_id) as executor:
        for node in plan.nodes:
            ctx.check_cancelled()
            result = executor.run(
                f"kubectl --kubeconfig /etc/kubernetes/admin.conf drain {shlex.quote(node.name)} "
                "--ignore-daemonsets --delete-emptydir-data --force --timeout=300s",
                CommandOptions(sudo=True, timeout_seconds=360),
            )
            output = (result.stdout + result.stderr).strip()
            if result.ok:
                ctx.info(f"[{node.name}] drained")
            elif "not found" in output:
                ctx.info(f"[{node.name}] not registered in the cluster; nothing to drain")
            elif plan.force:
                ctx.warning(f"[{node.name}] drain failed, continuing (force): {output[-300:]}")
            else:
                raise StepFailedError(
                    f"Draining {node.name} failed (a PodDisruptionBudget may block eviction): {output[-500:]}",
                    code="drain_failed",
                )
    return {}


def reset_removed(ctx: StepContext) -> dict[str, Any]:
    """kubeadm reset on the leaving nodes (a control plane also leaves etcd here)."""
    plan = _plan(ctx)
    _for_nodes(
        ctx,
        plan.nodes,
        _script_action(ctx, "reset.sh", {"VIP": cfg.vip_of(plan.spec) or ""}, action="kubeadm reset", timeout=600),
        parallel=False,
        tolerate_unreachable=plan.force,
    )
    return {}


ETCDCTL = (
    "etcdctl --endpoints=https://127.0.0.1:2379 --cacert=/etc/kubernetes/pki/etcd/ca.crt "
    "--cert=/etc/kubernetes/pki/etcd/server.crt --key=/etc/kubernetes/pki/etcd/server.key"
)


def remove_etcd_members(ctx: StepContext) -> dict[str, Any]:
    """Make sure leaving control planes are no longer etcd members (kubeadm reset normally did it;
    this covers unreachable nodes)."""
    plan = _plan(ctx)
    leaving = {n.name for n in plan.control_planes}
    with connect(ctx, plan.lead.machine_id) as executor:
        pod = f"etcd-{plan.lead.name}"
        listed = json.loads(_kubectl(executor, f"-n kube-system exec {pod} -- {ETCDCTL} member list -w json"))
        removed = []
        for member in listed.get("members") or []:
            if member.get("name") in leaving:
                member_id = format(int(member["ID"]), "x")
                _kubectl(executor, f"-n kube-system exec {pod} -- {ETCDCTL} member remove {member_id}")
                removed.append(member["name"])
                ctx.info(f"[{member['name']}] removed from etcd (member {member_id})")
        remaining = len(listed.get("members") or []) - len(removed)
    if not removed:
        ctx.info("Leaving control planes already left etcd")
    ctx.info(f"etcd has {remaining} member(s)")
    return {"removed": removed, "members": remaining}


def delete_node_objects(ctx: StepContext) -> dict[str, Any]:
    plan = _plan(ctx)
    with connect(ctx, plan.lead.machine_id) as executor:
        for node in plan.nodes:
            _kubectl(executor, f"delete node {shlex.quote(node.name)} --ignore-not-found --wait=false")
            ctx.info(f"[{node.name}] node object deleted")
    return {}


def finalize_remove(ctx: StepContext) -> dict[str, Any]:
    plan = _plan(ctx)
    ids = [n.machine_id for n in plan.nodes]
    with ctx.db() as session:
        cluster = session.get(Cluster, plan.cluster_id)
        if cluster is not None:
            cluster.spec = plan.spec.model_dump(mode="json")
        session.execute(
            delete(ClusterMachine).where(
                ClusterMachine.cluster_id == plan.cluster_id, ClusterMachine.machine_id.in_(ids)
            )
        )
    names = [n.name for n in plan.nodes]
    ctx.info(f"Removed {', '.join(names)} from {plan.cluster_name}; machine(s) released to the inventory")
    ctx.set_result({"cluster": plan.cluster_name, "removed_nodes": names})
    return {"removed": names}
