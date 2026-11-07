"""Local provider: Linux "machines" as containers on the operator's workstation.

Each machine is a privileged systemd container (Ubuntu 24.04 + OpenSSH + a sudo
user), the same approach Kind uses for its nodes. The platform then treats it like
any other server reached over SSH: inspection, kubeadm installation and cluster
lifecycle are unchanged. Intended for development, demos and learning — not for
production workloads.

Security: access to the Docker daemon is equivalent to root on the Docker host,
and the containers run privileged. Only configure this provider on a workstation
or lab host you control.
"""

from __future__ import annotations

import hashlib
import io
import ipaddress
import socket
import tarfile
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.modules.providers.base import InfrastructureProvider
from app.modules.providers.http import ProviderApiError, ProviderUnreachableError
from app.modules.providers.types import (
    ConnectionCheckResult,
    DiscoveredMachine,
    DiskInfo,
    Endpoint,
    HardwareSpec,
    ImplementationStatus,
    MachineAddress,
    MachineRef,
    MachineSpecRequest,
    MachineStatus,
    OSInfo,
    PowerAction,
    ProviderCapability,
    ProviderCategory,
    ProviderType,
)

NODE_IMAGE_DIR = Path(__file__).with_name("local_node")
NODE_IMAGE_REPO = "kplatform/local-node"
LABEL_MANAGED = "io.kplatform.local-node"
LABEL_NETWORK = "io.kplatform.network"
SSH_PORT = "22/tcp"
API_PORT = "6443/tcp"


def node_image_tag() -> str:
    """Content-addressed tag: changing the Dockerfile or entrypoint builds a new image."""
    digest = hashlib.sha256()
    for name in ("Dockerfile", "entrypoint.sh"):
        digest.update((NODE_IMAGE_DIR / name).read_bytes().replace(b"\r\n", b"\n"))
    return f"{NODE_IMAGE_REPO}:ubuntu-24.04-{digest.hexdigest()[:12]}"


def _build_context() -> io.BytesIO:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name in ("Dockerfile", "entrypoint.sh"):
            data = (NODE_IMAGE_DIR / name).read_bytes().replace(b"\r\n", b"\n")
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755 if name.endswith(".sh") else 0o644
            tar.addfile(info, io.BytesIO(data))
    buffer.seek(0)
    return buffer


class LocalConfig(BaseModel):
    docker_host: str | None = Field(
        default=None,
        pattern=r"^(unix|npipe)://.+$",
        description="Docker daemon socket (unix:// or npipe://). Empty = the default for this system / DOCKER_HOST.",
    )
    network: str = Field(
        default="kplatform-local",
        pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}$",
        description="Docker network shared by the machines (created if missing)",
    )
    subnet: str | None = Field(
        default=None,
        pattern=r"^\d{1,3}(\.\d{1,3}){3}/(1[6-9]|2[0-8])$",
        description="Subnet for the network (default: a free 172.28-31.x.0/24). Machines get fixed addresses in it.",
    )
    name_prefix: str = Field(
        default="kplatform-",
        pattern=r"^[a-z0-9][a-z0-9-]{0,20}$",
        description="Container name prefix (machine 'node-1' becomes 'kplatform-node-1')",
    )
    access: Literal["published_ports", "container_network"] = Field(
        default="published_ports",
        description=(
            "How the platform reaches the machines: through ports published on 127.0.0.1 (platform runs on this "
            "host) or directly on the Docker network (platform runs in a container attached to it)"
        ),
    )
    enforce_resources: bool = Field(default=True, description="Apply the requested CPU and memory as container limits")


class LocalProvider(InfrastructureProvider):
    provider_type = ProviderType.LOCAL
    display_name = "Local (Docker)"
    description = (
        "Linux machines as privileged systemd containers on this workstation (like Kind nodes). "
        "For development, demos and learning."
    )
    category = ProviderCategory.LOCAL
    capabilities = frozenset(
        {
            ProviderCapability.VALIDATE_CONNECTION,
            ProviderCapability.DISCOVER,
            ProviderCapability.PROVISION,
            ProviderCapability.DELETE,
            ProviderCapability.STATUS,
            ProviderCapability.SPECIFICATIONS,
            ProviderCapability.POWER,
        }
    )
    implementation_status = ImplementationStatus.AVAILABLE
    config_model = LocalConfig
    credential_required = False
    container_machines = True

    def __init__(self, config: BaseModel, credential: bytes | None) -> None:
        super().__init__(config, credential)
        self._client: Any = None

    @property
    def cfg(self) -> LocalConfig:
        return self.config  # type: ignore[return-value]

    # ---- docker plumbing --------------------------------------------------------

    def _docker(self) -> Any:
        if self._client is None:
            import docker

            try:
                self._client = (
                    docker.DockerClient(base_url=self.cfg.docker_host, timeout=120)
                    if self.cfg.docker_host
                    else docker.from_env(timeout=120)
                )
                self._client.ping()
            except docker.errors.DockerException as exc:
                self._client = None
                raise ProviderUnreachableError(f"Cannot reach the Docker daemon: {exc}") from exc
        return self._client

    def _call(self, what: str, fn: Any, *args: Any, **kwargs: Any) -> Any:
        import docker

        try:
            return fn(*args, **kwargs)
        except docker.errors.NotFound:
            raise
        except docker.errors.APIError as exc:
            raise ProviderApiError(f"{what}: {exc.explanation or exc}", status=exc.status_code or 0) from exc
        except docker.errors.DockerException as exc:
            raise ProviderUnreachableError(f"{what}: {exc}") from exc

    def _container(self, external_id: str) -> Any | None:
        import docker

        try:
            container = self._call("Inspect container", self._docker().containers.get, external_id)
        except docker.errors.NotFound:
            return None
        if container.labels.get(LABEL_MANAGED) != "true":
            raise ProviderApiError(f"Container '{external_id}' is not a platform-managed local node")
        return container

    def _ensure_network(self) -> Any:
        """The shared network, with a platform-chosen subnet so machines keep fixed IPs.

        Docker assigns container addresses dynamically at every start; a Kubernetes
        node whose IP changes after a restart breaks its cluster. Static addresses
        require a user-configured subnet, so the platform creates the network itself.
        """
        import docker
        from docker.types import IPAMConfig, IPAMPool

        client = self._docker()
        try:
            network = client.networks.get(self.cfg.network)
        except docker.errors.NotFound:
            subnet = ipaddress.ip_network(self.cfg.subnet or self._free_subnet(), strict=False)
            ipam = IPAMConfig(pool_configs=[IPAMPool(subnet=str(subnet), gateway=str(next(subnet.hosts())))])
            network = self._call(
                "Create network",
                client.networks.create,
                self.cfg.network,
                driver="bridge",
                ipam=ipam,
                labels={LABEL_NETWORK: "true"},
            )
            network.reload()
        if not self._subnet(network):
            raise ProviderApiError(
                f"Docker network '{self.cfg.network}' has no configured subnet; machines need fixed addresses. "
                "Choose another network name and the platform will create it."
            )
        return network

    @staticmethod
    def _subnet(network: Any) -> ipaddress.IPv4Network | None:
        for pool in (network.attrs.get("IPAM") or {}).get("Config") or []:
            try:
                net = ipaddress.ip_network(pool.get("Subnet", ""), strict=False)
            except ValueError:
                continue
            if isinstance(net, ipaddress.IPv4Network):
                return net
        return None

    def _free_subnet(self) -> str:
        used = []
        for network in self._call("List networks", self._docker().networks.list):
            network.reload()
            subnet = self._subnet(network)
            if subnet:
                used.append(subnet)
        for second in range(28, 32):
            for third in range(256):
                candidate = ipaddress.ip_network(f"172.{second}.{third}.0/24")
                if not any(candidate.overlaps(u) for u in used):
                    return str(candidate)
        raise ProviderApiError("No free 172.28.0.0/14 subnet found; set 'subnet' in the connection configuration")

    def _free_ip(self, network: Any) -> str:
        network.reload()
        subnet = self._subnet(network)
        assert subnet is not None
        taken = {
            str(ipaddress.ip_interface(c["IPv4Address"]).ip)
            for c in (network.attrs.get("Containers") or {}).values()
            if c.get("IPv4Address")
        }
        # Also reserve addresses of stopped platform containers (not listed while stopped).
        for container in self._call(
            "List containers",
            self._docker().containers.list,
            all=True,
            filters={"label": f"{LABEL_NETWORK}={self.cfg.network}"},
        ):
            fixed = container.labels.get("io.kplatform.ip")
            if fixed:
                taken.add(fixed)
        hosts = list(subnet.hosts())
        for ip in hosts[9:]:  # leave .1-.9 for the gateway and manual use
            if str(ip) not in taken:
                return str(ip)
        raise ProviderApiError(f"No free address left in {subnet}")

    @staticmethod
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            return int(probe.getsockname()[1])

    def ensure_image(self) -> str:
        import docker

        tag = node_image_tag()
        client = self._docker()
        try:
            client.images.get(tag)
        except docker.errors.ImageNotFound:
            try:
                client.images.build(fileobj=_build_context(), custom_context=True, tag=tag, rm=True, pull=True)
            except docker.errors.BuildError as exc:
                tail = " ".join(str(line.get("stream", "")).strip() for line in list(exc.build_log)[-5:])
                raise ProviderApiError(f"Building the node image failed: {exc.msg} {tail}".strip()) from exc
            except docker.errors.APIError as exc:
                raise ProviderApiError(f"Building the node image failed: {exc.explanation or exc}") from exc
        return tag

    def _describe(self, container: Any) -> DiscoveredMachine:
        container.reload()
        attrs = container.attrs
        settings = attrs.get("NetworkSettings") or {}
        network = (settings.get("Networks") or {}).get(self.cfg.network) or {}
        ip = network.get("IPAddress") or container.labels.get("io.kplatform.ip", "")
        ports = settings.get("Ports") or {}

        def published(port: str) -> Endpoint | None:
            if self.cfg.access == "container_network":
                return Endpoint(host=ip, port=int(port.split("/")[0])) if ip else None
            bindings = ports.get(port) or ((attrs.get("HostConfig") or {}).get("PortBindings") or {}).get(port) or []
            if not bindings or not bindings[0].get("HostPort"):
                return None
            host = bindings[0].get("HostIp") or "127.0.0.1"
            return Endpoint(
                host="127.0.0.1" if host in ("0.0.0.0", "::", "") else host, port=int(bindings[0]["HostPort"])
            )

        host_config = attrs.get("HostConfig") or {}
        nano = host_config.get("NanoCpus") or 0
        memory = host_config.get("Memory") or 0
        return DiscoveredMachine(
            external_id=container.name,
            name=container.labels.get("io.kplatform.machine-name") or container.name,
            addresses=[MachineAddress(type="internal", address=ip)] if ip else [],
            power_state="running" if container.status == "running" else "stopped",
            hardware=HardwareSpec(
                cpu_cores=int(nano / 1e9) if nano else None,
                memory_mb=int(memory / (1024 * 1024)) if memory else None,
            ),
            os=OSInfo(family="debian", distribution="ubuntu", version="24.04"),
            provider_metadata={
                "container_id": container.id[:12],
                "image": (attrs.get("Config") or {}).get("Image", ""),
                "network": self.cfg.network,
            },
            ssh_endpoint=published(SSH_PORT),
            kube_api_endpoint=published(API_PORT),
        )

    # ---- capabilities -------------------------------------------------------------

    def validate_connection(self) -> ConnectionCheckResult:
        client = self._docker()
        version = self._call("Read Docker version", client.version)
        info = self._call("Read Docker info", client.info)
        import docker

        try:
            client.images.get(node_image_tag())
            image = "present"
        except docker.errors.ImageNotFound:
            image = "built on first provision (needs internet access)"
        return ConnectionCheckResult(
            ok=True,
            message=f"Docker {version.get('Version')} on {info.get('OperatingSystem')}",
            details={
                "docker_version": str(version.get("Version")),
                "os": str(info.get("OperatingSystem")),
                "cpus": str(info.get("NCPU")),
                "memory_mb": str(int(info.get("MemTotal", 0) / (1024 * 1024))),
                "cgroup_version": str(info.get("CgroupVersion")),
                "node_image": image,
            },
        )

    def discover(self) -> list[DiscoveredMachine]:
        containers = self._call(
            "List containers",
            self._docker().containers.list,
            all=True,
            filters={"label": [f"{LABEL_MANAGED}=true", f"{LABEL_NETWORK}={self.cfg.network}"]},
        )
        return [self._describe(c) for c in containers]

    def provision(self, request: MachineSpecRequest) -> DiscoveredMachine:
        name = f"{self.cfg.name_prefix}{request.name}"
        existing = self._container(name)
        if existing is not None:  # idempotent: a retried step finds the container it created
            if existing.status != "running":
                self._call("Start container", existing.start)
            return self._describe(existing)
        if not request.ssh_public_key:
            raise ProviderApiError("A public SSH key is required to provision a local machine")

        image = self.ensure_image()
        network = self._ensure_network()
        client = self._docker()
        ip = self._free_ip(network)
        kwargs: dict[str, Any] = {
            "name": name,
            "hostname": request.name,
            "detach": True,
            "privileged": True,
            "cgroupns": "private",
            "security_opt": ["seccomp=unconfined", "apparmor=unconfined"],
            "tmpfs": {"/run": "", "/run/lock": "", "/tmp": ""},
            "volumes": {
                "/lib/modules": {"bind": "/lib/modules", "mode": "ro"},
                f"{name}-var": {"bind": "/var", "mode": "rw"},
            },
            "environment": {"AUTHORIZED_KEY": request.ssh_public_key, "NODE_USER": request.ssh_username},
            "network": self.cfg.network,
            "networking_config": {self.cfg.network: client.api.create_endpoint_config(ipv4_address=ip)},
            "labels": {
                LABEL_MANAGED: "true",
                LABEL_NETWORK: self.cfg.network,
                "io.kplatform.machine-name": request.name,
                "io.kplatform.ip": ip,
                **{f"io.kplatform.label.{k}": v for k, v in request.labels.items()},
            },
            "restart_policy": {"Name": "unless-stopped"},
        }
        if self.cfg.access == "published_ports":
            # Fixed host ports: Docker re-randomizes ephemeral ports on every restart.
            kwargs["ports"] = {SSH_PORT: ("127.0.0.1", self._free_port()), API_PORT: ("127.0.0.1", self._free_port())}
        if self.cfg.enforce_resources:
            kwargs["nano_cpus"] = int(request.cpu_cores * 1e9)
            kwargs["mem_limit"] = f"{request.memory_mb}m"
        container = self._call("Create container", client.containers.run, image, **kwargs)
        return self._describe(container)

    def delete(self, machine: MachineRef) -> None:
        import docker

        container = self._container(machine.external_id)
        if container is not None:
            self._call("Remove container", container.remove, force=True, v=False)
        try:
            volume = self._docker().volumes.get(f"{machine.external_id}-var")
            self._call("Remove volume", volume.remove, force=True)
        except docker.errors.NotFound:
            pass

    def get_status(self, machine: MachineRef) -> MachineStatus:
        container = self._container(machine.external_id)
        if container is None:
            return MachineStatus(external_id=machine.external_id, exists=False, power_state="unknown")
        described = self._describe(container)
        return MachineStatus(
            external_id=machine.external_id,
            exists=True,
            power_state=described.power_state,
            addresses=described.addresses,
            ssh_endpoint=described.ssh_endpoint,
            kube_api_endpoint=described.kube_api_endpoint,
        )

    def get_specifications(self, machine: MachineRef) -> HardwareSpec:
        container = self._container(machine.external_id)
        if container is None:
            raise ProviderApiError(f"Container '{machine.external_id}' does not exist", status=404)
        spec = self._describe(container).hardware or HardwareSpec()
        spec.disks = [DiskInfo(name=f"{machine.external_id}-var", size_gb=0, type="docker-volume")]
        return spec

    def set_power_state(self, machine: MachineRef, action: PowerAction) -> MachineStatus:
        container = self._container(machine.external_id)
        if container is None:
            raise ProviderApiError(f"Container '{machine.external_id}' does not exist", status=404)
        if action is PowerAction.START:
            self._call("Start container", container.start)
        elif action is PowerAction.STOP:
            self._call("Stop container", container.stop, timeout=60)
        else:
            self._call("Restart container", container.restart, timeout=60)
        return self.get_status(machine)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
