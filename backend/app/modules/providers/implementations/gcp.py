"""Google Cloud provider: Compute Engine inventory and instance lifecycle operations.

Authentication follows Google's service-account flow: a JWT signed (RS256) with
the key's private key is exchanged for an access token. No Google SDK is required.
Instance provisioning requires Compute Engine create/get/delete permissions.

Status: PREVIEW — tested against recorded API responses, not yet verified against a
live project.
"""

from __future__ import annotations

import base64
import json
import time
from typing import Any
from urllib.parse import quote
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, Field

from app.modules.credentials.models import CredentialKind
from app.modules.providers import http
from app.modules.providers.base import InfrastructureProvider
from app.modules.providers.http import ProviderApiError
from app.modules.providers.implementations.cloud_actions import CloudMachineActions
from app.modules.providers.implementations.managed_actions import ManagedClusterActions
from app.modules.providers.types import (
    ConnectionCheckResult,
    DiscoveredMachine,
    HardwareSpec,
    ImplementationStatus,
    MachineAddress,
    MachineRef,
    MachineSpecRequest,
    MachineStatus,
    ProviderCapability,
    ProviderCategory,
    ProviderType,
)

COMPUTE = "https://compute.googleapis.com/compute/v1"
SCOPE = "https://www.googleapis.com/auth/cloud-platform"
COMPUTE_SCOPE = "https://www.googleapis.com/auth/compute"
CONTAINER = "https://container.googleapis.com/v1"
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"
MAX_PAGES = 50


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def service_account_jwt(key: dict[str, Any], *, scope: str, now: int | None = None) -> str:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    issued = int(now if now is not None else time.time())
    header = {"alg": "RS256", "typ": "JWT", "kid": key.get("private_key_id", "")}
    claims = {
        "iss": key["client_email"],
        "scope": scope,
        "aud": key.get("token_uri") or DEFAULT_TOKEN_URI,
        "iat": issued,
        "exp": issued + 3600,
    }
    signing_input = f"{_b64url(json.dumps(header).encode())}.{_b64url(json.dumps(claims).encode())}"
    private_key = serialization.load_pem_private_key(key["private_key"].encode(), password=None)
    if not isinstance(private_key, rsa.RSAPrivateKey):
        raise ProviderApiError("The service account key is not an RSA key")
    signature = private_key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input}.{_b64url(signature)}"


def _gcp_error(response: http.HttpResponse) -> str:
    try:
        body = response.json() or {}
    except ValueError:
        return response.text[:300]
    error = body.get("error")
    if isinstance(error, dict):
        return str(error.get("message", ""))
    if isinstance(error, str):
        return f"{error}: {body.get('error_description', '')}".strip(": ")
    return response.text[:300]


class GcpConfig(BaseModel):
    project_id: str | None = Field(
        default=None,
        pattern=r"^[a-z][-a-z0-9]{4,28}[a-z0-9]$",
        description="Project to inventory (default: the service account's project)",
    )
    zones: list[str] = Field(default_factory=list, description="Limit inventory to these zones (default: all)")


class GcpProvider(CloudMachineActions, ManagedClusterActions, InfrastructureProvider):
    provider_type = ProviderType.GCP
    display_name = "Google Cloud"
    description = "Compute Engine lifecycle management and GKE managed Kubernetes provisioning."
    category = ProviderCategory.CLOUD
    capabilities = frozenset(
        {
            ProviderCapability.VALIDATE_CONNECTION,
            ProviderCapability.DISCOVER,
            ProviderCapability.PROVISION,
            ProviderCapability.DELETE,
            ProviderCapability.STATUS,
            ProviderCapability.POWER,
            ProviderCapability.NETWORK,
            ProviderCapability.MANAGED_KUBERNETES,
        }
    )
    implementation_status = ImplementationStatus.PREVIEW
    config_model = GcpConfig
    credential_kinds = frozenset({CredentialKind.GCP_SERVICE_ACCOUNT})

    def __init__(self, config: BaseModel, credential: bytes | None) -> None:
        super().__init__(config, credential)
        self._token: tuple[str, float, str] | None = None

    @property
    def cfg(self) -> GcpConfig:
        return self.config  # type: ignore[return-value]

    def _key(self) -> dict[str, Any]:
        try:
            key = json.loads((self._credential or b"{}").decode())
        except ValueError:
            raise ProviderApiError("The service account key is not valid JSON") from None
        if key.get("type") != "service_account" or not key.get("private_key") or not key.get("client_email"):
            raise ProviderApiError("The credential is not a complete service account key")
        return dict(key)

    @property
    def project(self) -> str:
        project = self.cfg.project_id or self._key().get("project_id")
        if not project:
            raise ProviderApiError("No project configured and the key does not name one")
        return str(project)

    def _access_token(self, scope: str = COMPUTE_SCOPE) -> str:
        if self._token and self._token[2] == scope and self._token[1] > time.time() + 60:
            return self._token[0]
        key = self._key()
        response = http.send(
            "POST",
            key.get("token_uri") or DEFAULT_TOKEN_URI,
            form={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": service_account_jwt(key, scope=scope),
            },
        )
        if response.status in (400, 401):
            raise http.ProviderAuthError(f"Google sign-in failed: {_gcp_error(response)}", status=response.status)
        http.expect_ok(response, "Google sign-in", message_of=_gcp_error)
        body = response.json()
        self._token = (str(body["access_token"]), time.time() + int(body.get("expires_in", 3600)), scope)
        return self._token[0]

    def _get(self, url: str, what: str, params: dict[str, Any] | None = None) -> Any:
        response = http.send("GET", url, headers={"Authorization": f"Bearer {self._access_token()}"}, params=params)
        http.expect_ok(response, f"Google Cloud {what}", message_of=_gcp_error)
        return response.json()

    def _request(self, method: str, url: str, what: str, body: dict[str, Any] | None = None) -> Any:
        response = http.send(method, url, headers={"Authorization": f"Bearer {self._access_token()}"}, json_body=body)
        http.expect_ok(response, f"Google Cloud {what}", message_of=_gcp_error)
        return response.json() if response.body else {}

    def _gke_get(self, location: str, name: str) -> dict[str, Any] | None:
        url = (
            f"{CONTAINER}/projects/{quote(self.project, safe='')}/locations/{quote(location, safe='')}"
            f"/clusters/{quote(name, safe='')}"
        )
        response = http.send("GET", url, headers={"Authorization": f"Bearer {self._access_token(SCOPE)}"})
        if response.status == 404:
            return None
        http.expect_ok(response, "Google Cloud read GKE cluster", message_of=_gcp_error)
        return response.json()

    def managed_cluster_id(self, name: str, options: dict[str, object]) -> str:
        location = str(options.get("managed_location") or "")
        if not location:
            raise ProviderApiError("GKE needs a managed location")
        return f"{self.project}/{location}/{name}"

    def _gke_wait_operation(self, location: str, operation: str) -> None:
        name = operation.rsplit("/", 1)[-1]
        url = (
            f"{CONTAINER}/projects/{quote(self.project, safe='')}/locations/{quote(location, safe='')}"
            f"/operations/{quote(name, safe='')}"
        )
        deadline = time.monotonic() + 3600
        while time.monotonic() < deadline:
            response = http.send("GET", url, headers={"Authorization": f"Bearer {self._access_token(SCOPE)}"})
            http.expect_ok(response, "Google Cloud wait for GKE operation", message_of=_gcp_error)
            result = response.json()
            if result.get("status") == "DONE":
                if result.get("error"):
                    raise ProviderApiError(f"GKE operation failed: {result['error']}")
                return
            time.sleep(10)
        raise ProviderApiError("Timed out waiting for GKE operation")

    def _gke_request(self, method: str, url: str, what: str, body: dict[str, Any] | None = None) -> Any:
        response = http.send(
            method, url, headers={"Authorization": f"Bearer {self._access_token(SCOPE)}"}, json_body=body
        )
        http.expect_ok(response, f"Google Cloud {what}", message_of=_gcp_error)
        return response.json() if response.body else {}

    def gke_bearer_token(self) -> str:
        """Get a renewable service-account token for Kubernetes API requests."""
        return self._access_token(SCOPE)

    def create_managed_cluster(self, request) -> Any:
        from app.modules.providers.types import ManagedClusterResult

        options = request.options
        location = str(options.get("managed_location") or "")
        network = str(options.get("managed_network") or "")
        subnetwork = str(options.get("managed_subnetwork") or "")
        machine_type = str(options.get("managed_node_machine_type") or "e2-standard-2")
        if not location or not network or not subnetwork:
            raise ProviderApiError("GKE needs managed_location, managed_network and managed_subnetwork options")
        cluster_name = request.name
        path = f"{CONTAINER}/projects/{quote(self.project, safe='')}/locations/{quote(location, safe='')}/clusters"
        current = self._gke_get(location, cluster_name)
        if current is None:
            pools = []
            for pool in request.node_pools:
                count = int(pool.get("count") or 0)
                if count < 1:
                    continue
                pool_name = str(pool.get("name") or "workers")[:40]
                labels = {str(key): str(value) for key, value in (pool.get("labels") or {}).items()}
                labels["kubernetes-platform-pool"] = pool_name
                taints = [
                    {
                        "key": str(item["key"]),
                        "value": str(item.get("value") or ""),
                        "effect": {
                            "NoSchedule": "NO_SCHEDULE",
                            "PreferNoSchedule": "PREFER_NO_SCHEDULE",
                            "NoExecute": "NO_EXECUTE",
                        }[str(item["effect"])],
                    }
                    for item in (pool.get("taints") or [])
                ]
                pools.append(
                    {
                        "name": pool_name,
                        "initialNodeCount": count,
                        "config": {
                            "machineType": str(pool.get("machine_type") or machine_type),
                            "diskSizeGb": int(pool.get("disk_gb") or 100),
                            "oauthScopes": [SCOPE],
                            "labels": labels,
                            "taints": taints,
                        },
                        "management": {"autoRepair": True, "autoUpgrade": True},
                    }
                )
            if not pools:
                raise ProviderApiError("GKE needs at least one initial worker node")
            body = {
                "name": cluster_name,
                "initialClusterVersion": request.version.removeprefix("v"),
                "network": network,
                "subnetwork": subnetwork,
                "nodePools": pools,
                "resourceLabels": {"kubernetes-platform-managed": "true"},
                "ipAllocationPolicy": {
                    "useIpAliases": True,
                    "clusterIpv4CidrBlock": str(options.get("managed_pod_cidr") or "10.244.0.0/16"),
                    "servicesIpv4CidrBlock": str(options.get("managed_service_cidr") or "10.96.0.0/12"),
                },
            }
            if options.get("managed_cni") == "cilium":
                body["networkConfig"] = {"datapathProvider": "ADVANCED_DATAPATH"}
            operation = self._gke_request("POST", path, "create GKE cluster", {"cluster": body})
            self._gke_wait_operation(location, str(operation.get("name") or ""))
            current = self._gke_get(location, cluster_name)
        if not current or (current.get("resourceLabels") or {}).get("kubernetes-platform-managed") != "true":
            raise ProviderApiError("Refusing to use a GKE cluster without the platform ownership label")
        deadline = time.monotonic() + 3600
        while current.get("status") != "RUNNING" and time.monotonic() < deadline:
            time.sleep(10)
            current = self._gke_get(location, cluster_name) or {}
        if current.get("status") != "RUNNING":
            raise ProviderApiError("Timed out waiting for GKE cluster to become RUNNING")
        endpoint = str(current.get("endpoint") or "")
        ca_data = str((current.get("masterAuth") or {}).get("clusterCaCertificate") or "")
        if not endpoint or not ca_data:
            raise ProviderApiError("GKE returned an incomplete API endpoint or certificate authority")
        return ManagedClusterResult(
            external_id=f"{self.project}/{location}/{cluster_name}",
            endpoint=endpoint if endpoint.startswith("https://") else f"https://{endpoint}",
            certificate_authority_data=ca_data,
        )

    def delete_managed_cluster(self, external_id: str) -> None:
        project, separator, rest = external_id.partition("/")
        location, separator2, name = rest.partition("/")
        if not separator or not separator2 or project != self.project:
            raise ProviderApiError("GKE ID must contain project/location/name for this connection")
        cluster = self._gke_get(location, name)
        if cluster is None:
            return
        if (cluster.get("resourceLabels") or {}).get("kubernetes-platform-managed") != "true":
            raise ProviderApiError("Refusing to delete a GKE cluster without the platform ownership label")
        url = (
            f"{CONTAINER}/projects/{quote(project, safe='')}/locations/{quote(location, safe='')}"
            f"/clusters/{quote(name, safe='')}"
        )
        operation = self._gke_request("DELETE", url, "delete GKE cluster")
        self._gke_wait_operation(location, str(operation.get("name") or ""))
        deadline = time.monotonic() + 3600
        while time.monotonic() < deadline:
            if self._gke_get(location, name) is None:
                return
            time.sleep(10)
        raise ProviderApiError("Timed out deleting GKE cluster")

    def _instance(self, zone: str, name: str) -> dict[str, Any] | None:
        response = http.send(
            "GET",
            f"{COMPUTE}/projects/{self.project}/zones/{quote(zone, safe='')}/instances/{quote(name, safe='')}",
            headers={"Authorization": f"Bearer {self._access_token()}"},
        )
        if response.status == 404:
            return None
        http.expect_ok(response, "Google Cloud read instance", message_of=_gcp_error)
        return response.json()

    @staticmethod
    def _machine(zone: str, instance: dict[str, Any]) -> DiscoveredMachine:
        addresses: list[MachineAddress] = []
        for nic in instance.get("networkInterfaces", []):
            if nic.get("networkIP"):
                addresses.append(MachineAddress(type="internal", address=str(nic["networkIP"])))
            addresses.extend(
                MachineAddress(type="external", address=str(access["natIP"]))
                for access in nic.get("accessConfigs", [])
                if access.get("natIP")
            )
        state = str(instance.get("status", "UNKNOWN"))
        machine_type = str(instance.get("machineType", "")).rsplit("/", 1)[-1]
        return DiscoveredMachine(
            external_id=f"{zone}/{instance.get('name', '')}",
            name=str(instance.get("name", "")),
            addresses=addresses,
            power_state={"RUNNING": "running", "TERMINATED": "stopped", "STOPPED": "stopped"}.get(state, state.lower()),
            provider_metadata={"zone": zone, "machine_type": machine_type},
            importable=False,
        )

    def provision(self, request: MachineSpecRequest) -> DiscoveredMachine:
        options = request.provider_options
        zone = str(options.get("zone") or (self.cfg.zones[0] if self.cfg.zones else ""))
        image = request.image or options.get("image")
        machine_type = options.get("machine_type")
        network = options.get("network") or request.network
        if not zone or not image or not machine_type or not network or not request.ssh_public_key:
            raise ProviderApiError(
                "Google Cloud provisioning requires zone, image, machine_type, network and an SSH public key"
            )
        existing = self._instance(zone, request.name)
        if existing is not None:
            labels = existing.get("labels") or {}
            if labels.get("kplatform_managed") != "true":
                raise ProviderApiError(f"An unmanaged Compute Engine instance named '{request.name}' already exists")
            return self._machine(zone, existing)

        script = (
            "#!/bin/bash\nset -eu\n"
            f"id -u {request.ssh_username} >/dev/null 2>&1 || useradd --create-home --shell /bin/bash "
            f"{request.ssh_username}\n"
            f"install -d -m 700 -o {request.ssh_username} -g {request.ssh_username} "
            f"/home/{request.ssh_username}/.ssh\n"
            f"printf '%s\\n' {self._shell_quote(request.ssh_public_key)} "
            f"> /home/{request.ssh_username}/.ssh/authorized_keys\n"
            f"chmod 600 /home/{request.ssh_username}/.ssh/authorized_keys\n"
            f"chown {request.ssh_username}:{request.ssh_username} /home/{request.ssh_username}/.ssh/authorized_keys\n"
            f"printf '%s\\n' {self._shell_quote(request.ssh_username + ' ALL=(ALL) NOPASSWD:ALL')} "
            ">/etc/sudoers.d/90-platform\nchmod 440 /etc/sudoers.d/90-platform\n"
        )
        disk: dict[str, Any] = {
            "boot": True,
            "autoDelete": True,
            "initializeParams": {
                "sourceImage": str(image),
                "diskSizeGb": str(request.disk_gb),
            },
        }
        network_interface: dict[str, Any] = {"network": str(network)}
        if options.get("subnetwork"):
            network_interface["subnetwork"] = str(options["subnetwork"])
        if options.get("external_ip", True):
            network_interface["accessConfigs"] = [{"name": "External NAT", "type": "ONE_TO_ONE_NAT"}]
        body = {
            "name": request.name,
            "machineType": str(machine_type)
            if str(machine_type).startswith("https://")
            else f"zones/{zone}/machineTypes/{machine_type}",
            "disks": [disk],
            "networkInterfaces": [network_interface],
            "labels": {"kplatform_managed": "true"},
            "metadata": {"items": [{"key": "startup-script", "value": script}]},
        }
        request_id = uuid5(NAMESPACE_URL, f"{self.project}/{zone}/{request.name}")
        self._request(
            "POST",
            f"{COMPUTE}/projects/{self.project}/zones/{quote(zone, safe='')}/instances?requestId={request_id}",
            "create instance",
            body,
        )
        created = self._instance(zone, request.name)
        return (
            self._machine(zone, created)
            if created
            else DiscoveredMachine(
                external_id=f"{zone}/{request.name}",
                name=request.name,
                power_state="pending",
                provider_metadata={"zone": zone},
                importable=False,
            )
        )

    @staticmethod
    def _shell_quote(value: str) -> str:
        return "'" + value.replace("'", "'\\''") + "'"

    def get_status(self, machine: MachineRef) -> MachineStatus:
        zone, separator, name = machine.external_id.partition("/")
        if not separator or not zone or not name:
            raise ProviderApiError("Google Cloud machine ID must contain zone/name")
        instance = self._instance(zone, name)
        found = self._machine(zone, instance) if instance else None
        return MachineStatus(
            external_id=machine.external_id,
            exists=found is not None,
            power_state=found.power_state if found else "deleted",
            addresses=found.addresses if found else [],
        )

    def delete(self, machine: MachineRef) -> None:
        zone, separator, name = machine.external_id.partition("/")
        if not separator or not zone or not name:
            raise ProviderApiError("Google Cloud machine ID must contain zone/name")
        instance = self._instance(zone, name)
        if instance is None:
            return
        if (instance.get("labels") or {}).get("kplatform_managed") != "true":
            raise ProviderApiError("Refusing to delete a Compute Engine instance without the platform ownership label")
        self._request(
            "DELETE",
            f"{COMPUTE}/projects/{self.project}/zones/{quote(zone, safe='')}/instances/{quote(name, safe='')}",
            "delete instance",
        )

    def validate_connection(self) -> ConnectionCheckResult:
        key = self._key()
        project = self._get(f"{COMPUTE}/projects/{self.project}", "read project")
        details = {
            "project": str(project.get("name", self.project)),
            "service_account": str(key.get("client_email")),
        }
        return ConnectionCheckResult(ok=True, message=f"Project {details['project']}", details=details)

    def discover(self) -> list[DiscoveredMachine]:
        instances: list[dict[str, Any]] = []
        token: str | None = None
        for _ in range(MAX_PAGES):
            params: dict[str, Any] = {"maxResults": 500, "pageToken": token}
            page = self._get(f"{COMPUTE}/projects/{self.project}/aggregated/instances", "list instances", params)
            for scope, entry in (page.get("items") or {}).items():
                zone = scope.removeprefix("zones/")
                if self.cfg.zones and zone not in self.cfg.zones:
                    continue
                instances.extend(entry.get("instances", []))
            token = page.get("nextPageToken")
            if not token:
                break

        types: dict[tuple[str, str], tuple[int, int]] = {}
        machines = []
        for instance in instances:
            zone = str(instance.get("zone", "")).rsplit("/", 1)[-1]
            machine_type = str(instance.get("machineType", "")).rsplit("/", 1)[-1]
            if (zone, machine_type) not in types and machine_type and not machine_type.startswith("custom-"):
                described = self._get(
                    f"{COMPUTE}/projects/{self.project}/zones/{zone}/machineTypes/{machine_type}", "read machine type"
                )
                types[(zone, machine_type)] = (int(described.get("guestCpus", 0)), int(described.get("memoryMb", 0)))
            cores, memory = types.get((zone, machine_type), (0, 0))
            addresses: list[MachineAddress] = []
            for nic in instance.get("networkInterfaces", []):
                if nic.get("networkIP"):
                    addresses.append(MachineAddress(type="internal", address=str(nic["networkIP"])))
                addresses.extend(
                    MachineAddress(type="external", address=str(ac["natIP"]))
                    for ac in nic.get("accessConfigs", [])
                    if ac.get("natIP")
                )
            status = str(instance.get("status", ""))
            machines.append(
                DiscoveredMachine(
                    external_id=f"{zone}/{instance.get('name')}",
                    name=str(instance.get("name", "")),
                    addresses=addresses,
                    power_state={"RUNNING": "running", "TERMINATED": "stopped", "STOPPED": "stopped"}.get(
                        status, status.lower() or "unknown"
                    ),
                    hardware=HardwareSpec(cpu_cores=cores or None, memory_mb=memory or None),
                    provider_metadata={"zone": zone, "machine_type": machine_type, "project": self.project},
                    importable=False,
                )
            )
        return machines
