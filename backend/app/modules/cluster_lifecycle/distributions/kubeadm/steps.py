"""Workflow steps for kubeadm cluster creation and deletion.

Every step is idempotent (scripts check state before acting) so the engine can
resume after a crash or a manual retry. Secrets generated here (bootstrap tokens,
certificate keys) are created per step, registered for log redaction, passed to
nodes over stdin and never stored in step outputs.
"""

from __future__ import annotations

import base64
import json
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
from app.modules.infrastructure.models import Machine
from app.orchestration.context import StepContext
from app.orchestration.workflow import RetryableStepError, StepFailedError

MAX_PARALLEL_NODES = 8


@cache
def _script(name: str) -> str:
    base = resources.files("app.modules.cluster_lifecycle.distributions.kubeadm") / "scripts"
    return (base / "lib.sh").read_text() + "\n" + (base / name).read_text()


def _plan(ctx: StepContext) -> ClusterPlanInput:
    return ctx.params  # type: ignore[return-value]


def _options(plan: ClusterPlanInput) -> cfg.KubeadmOptions:
    return cfg.KubeadmOptions.model_validate(plan.spec.options)


def _ip(ctx: StepContext, node: PlannedNode) -> str:
    return ctx.outputs["validate"]["nodes"][str(node.machine_id)]["ip"]


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
        ctx.info(
            f"[{node.name}] {facts.get('os_pretty_name')}, {facts.get('cpu_cores')} CPU, "
            f"{facts.get('memory_mb')} MB, address {facts.get('primary_ip')}"
        )
        return {
            "ip": facts.get("primary_ip"),
            "os": facts.get("os_pretty_name"),
            "hostname": facts.get("hostname"),
        }

    nodes = _for_nodes(ctx, plan.nodes, check)
    ips = [n["ip"] for n in nodes.values()]
    if len(set(ips)) != len(ips):
        problems.append("two or more machines report the same primary IP address")
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

    def run(node: PlannedNode, executor: HostExecutor) -> None:
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
    with connect(ctx, plan.control_planes[0].machine_id) as cp:
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
    if allow_cp_workloads is None:
        allow_cp_workloads = not plan.workers

    with connect(ctx, plan.control_planes[0].machine_id) as executor:
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
    with connect(ctx, plan.control_planes[0].machine_id) as executor:
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
        if (
            not cfg.control_plane_endpoint(plan.spec)
            and published.get("kube_api_host")
            and not cluster.api_server_override
        ):
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
        _script_action(ctx, "reset.sh", {}, action="kubeadm reset", timeout=600),
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
