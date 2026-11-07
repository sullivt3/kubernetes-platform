"""In-memory stand-in for the parts of the Docker SDK the local provider uses."""

from __future__ import annotations

import builtins
from typing import Any

import docker.errors


class FakeContainer:
    def __init__(self, engine: FakeDocker, name: str, image: str, kwargs: dict[str, Any]) -> None:
        self.engine = engine
        self.name = name
        self.image = image
        self.kwargs = kwargs
        self.labels: dict[str, str] = dict(kwargs.get("labels") or {})
        self.status = "running"
        self.id = (name.encode().hex() * 8)[:64]

    @property
    def ip(self) -> str:
        network = self.kwargs.get("network")
        endpoint = (self.kwargs.get("networking_config") or {}).get(network) or {}
        return str(endpoint.get("IPAMConfig", {}).get("IPv4Address", ""))

    @property
    def attrs(self) -> dict[str, Any]:
        network = self.kwargs.get("network")
        running = self.status == "running"
        ports = {
            port: [{"HostIp": host, "HostPort": str(host_port)}]
            for port, (host, host_port) in (self.kwargs.get("ports") or {}).items()
        }
        return {
            "Config": {"Image": self.image},
            "HostConfig": {
                "NanoCpus": self.kwargs.get("nano_cpus", 0),
                "Memory": int(str(self.kwargs.get("mem_limit", "0m")).rstrip("m")) * 1024 * 1024,
                "PortBindings": ports,
            },
            "NetworkSettings": {
                "Networks": {network: {"IPAddress": self.ip if running else ""}} if network else {},
                "Ports": ports if running else {},
            },
        }

    def reload(self) -> None:
        pass

    def start(self) -> None:
        self.status = "running"

    def stop(self, timeout: int = 10) -> None:
        self.status = "exited"

    def restart(self, timeout: int = 10) -> None:
        self.status = "running"

    def remove(self, force: bool = False, v: bool = False) -> None:
        self.engine.containers_by_name.pop(self.name, None)


class FakeNetwork:
    def __init__(self, engine: FakeDocker, name: str, subnet: str | None) -> None:
        self.engine = engine
        self.name = name
        self.subnet = subnet

    @property
    def attrs(self) -> dict[str, Any]:
        containers = {
            c.id: {"IPv4Address": f"{c.ip}/24"}
            for c in self.engine.containers_by_name.values()
            if c.kwargs.get("network") == self.name and c.status == "running" and c.ip
        }
        return {"IPAM": {"Config": [{"Subnet": self.subnet}] if self.subnet else []}, "Containers": containers}

    def reload(self) -> None:
        pass


class _Images:
    def __init__(self, engine: FakeDocker) -> None:
        self.engine = engine

    def get(self, tag: str) -> str:
        if tag not in self.engine.image_tags:
            raise docker.errors.ImageNotFound(f"No such image: {tag}")
        return tag

    def build(self, **kwargs: Any) -> tuple[str, list[Any]]:
        self.engine.builds.append(kwargs["tag"])
        self.engine.image_tags.add(kwargs["tag"])
        return kwargs["tag"], []


class _Networks:
    def __init__(self, engine: FakeDocker) -> None:
        self.engine = engine

    def get(self, name: str) -> FakeNetwork:
        if name not in self.engine.network_map:
            raise docker.errors.NotFound(f"network {name} not found")
        return self.engine.network_map[name]

    def create(self, name: str, driver: str, ipam: Any = None, labels: dict[str, str] | None = None) -> FakeNetwork:
        network = FakeNetwork(self.engine, name, ipam["Config"][0]["Subnet"] if ipam else None)
        self.engine.network_map[name] = network
        return network

    def list(self) -> builtins.list[FakeNetwork]:
        return builtins.list(self.engine.network_map.values())


class _Containers:
    def __init__(self, engine: FakeDocker) -> None:
        self.engine = engine

    def get(self, name: str) -> FakeContainer:
        if name not in self.engine.containers_by_name:
            raise docker.errors.NotFound(f"No such container: {name}")
        return self.engine.containers_by_name[name]

    def run(self, image: str, **kwargs: Any) -> FakeContainer:
        if image not in self.engine.image_tags:
            raise docker.errors.ImageNotFound(image)
        if kwargs.get("network") not in self.engine.network_map:
            raise docker.errors.APIError(f"network {kwargs.get('network')} not found")
        container = FakeContainer(self.engine, kwargs["name"], image, kwargs)
        self.engine.containers_by_name[container.name] = container
        self.engine.runs.append(kwargs)
        return container

    def list(self, all: bool = False, filters: dict[str, Any] | None = None) -> builtins.list[FakeContainer]:
        wanted = (filters or {}).get("label") or []
        wanted = [wanted] if isinstance(wanted, str) else wanted
        pairs = [w.partition("=") for w in wanted]
        return [
            c
            for c in self.engine.containers_by_name.values()
            if (all or c.status == "running") and builtins.all(c.labels.get(k) == v for k, _, v in pairs)
        ]


class _Volume:
    def __init__(self, engine: FakeDocker, name: str) -> None:
        self.engine = engine
        self.name = name

    def remove(self, force: bool = False) -> None:
        self.engine.volume_names.discard(self.name)
        self.engine.removed_volumes.append(self.name)


class _Volumes:
    def __init__(self, engine: FakeDocker) -> None:
        self.engine = engine

    def get(self, name: str) -> _Volume:
        if name not in self.engine.volume_names:
            raise docker.errors.NotFound(name)
        return _Volume(self.engine, name)


class _Api:
    @staticmethod
    def create_endpoint_config(ipv4_address: str) -> dict[str, Any]:
        return {"IPAMConfig": {"IPv4Address": ipv4_address}}


class FakeDocker:
    """Looks like ``docker.DockerClient`` for the calls the local provider makes."""

    def __init__(self) -> None:
        self.image_tags: set[str] = set()
        self.builds: list[str] = []
        self.network_map: dict[str, FakeNetwork] = {
            "bridge": FakeNetwork(self, "bridge", "172.17.0.0/16"),
            "kind": FakeNetwork(self, "kind", "172.28.0.0/24"),  # the provider must skip this subnet
        }
        self.containers_by_name: dict[str, FakeContainer] = {}
        self.volume_names: set[str] = set()
        self.removed_volumes: list[str] = []
        self.runs: list[dict[str, Any]] = []
        self.images = _Images(self)
        self.networks = _Networks(self)
        self.containers = _Containers(self)
        self.volumes = _Volumes(self)
        self.api = _Api()

    def ping(self) -> bool:
        return True

    def version(self) -> dict[str, Any]:
        return {"Version": "29.0.0"}

    def info(self) -> dict[str, Any]:
        return {"OperatingSystem": "FakeOS", "NCPU": 8, "MemTotal": 16 * 1024**3, "CgroupVersion": "2"}

    def close(self) -> None:
        pass
