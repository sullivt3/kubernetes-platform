"""Microsoft Azure provider: service principal validation and VM lifecycle management.

Authentication uses the OAuth 2.0 client-credentials flow against Microsoft Entra ID
and the Azure Resource Manager REST API; no Azure SDK is required. VM creation
uses a caller-supplied network interface and requires VM write/delete and network
interface join permissions.

Status: PREVIEW — tested against recorded API responses, not yet verified against a
live subscription.
"""

from __future__ import annotations

import base64
import time
from typing import Any
from urllib.parse import quote

import yaml
from pydantic import BaseModel, Field

from app.modules.credentials.models import CredentialKind
from app.modules.providers import http
from app.modules.providers.base import InfrastructureProvider
from app.modules.providers.types import (
    ConnectionCheckResult,
    DiscoveredMachine,
    HardwareSpec,
    ImplementationStatus,
    MachineAddress,
    MachineRef,
    MachineSpecRequest,
    MachineStatus,
    OSInfo,
    ProviderCapability,
    ProviderCategory,
    ProviderType,
)

LOGIN = "https://login.microsoftonline.com"
ARM = "https://management.azure.com"
GUID = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
API_SUBSCRIPTIONS = "2022-12-01"
API_COMPUTE = "2024-07-01"
API_NETWORK = "2024-05-01"
API_PUBLIC_IP = "2024-05-01"
API_MANAGED_K8S = "2024-05-01"
MAX_PAGES = 50


def _azure_error(response: http.HttpResponse) -> str:
    try:
        body = response.json() or {}
    except ValueError:
        return response.text[:300]
    error = body.get("error")
    if isinstance(error, dict):
        return f"{error.get('code', '')}: {error.get('message', '')}".strip(": ")
    if isinstance(error, str):  # Entra ID token endpoint
        description = str(body.get("error_description") or "").splitlines()
        return f"{error}: {description[0]}" if description else error
    return response.text[:300]


class AzureConfig(BaseModel):
    tenant_id: str = Field(pattern=GUID, description="Directory (tenant) ID")
    client_id: str = Field(pattern=GUID, description="Application (client) ID of the service principal")
    subscription_id: str = Field(pattern=GUID)
    resource_group: str | None = Field(
        default=None, min_length=1, max_length=90, description="Limit inventory to one resource group"
    )


class AzureProvider(InfrastructureProvider):
    provider_type = ProviderType.AZURE
    display_name = "Microsoft Azure"
    description = "Virtual machine lifecycle management and AKS managed Kubernetes provisioning."
    category = ProviderCategory.CLOUD
    capabilities = frozenset(
        {
            ProviderCapability.VALIDATE_CONNECTION,
            ProviderCapability.DISCOVER,
            ProviderCapability.PROVISION,
            ProviderCapability.DELETE,
            ProviderCapability.STATUS,
            ProviderCapability.MANAGED_KUBERNETES,
        }
    )
    planned_capabilities = frozenset({ProviderCapability.POWER, ProviderCapability.NETWORK})
    planned_capabilities_phase = 5
    implementation_status = ImplementationStatus.PREVIEW
    config_model = AzureConfig
    credential_kinds = frozenset({CredentialKind.AZURE_SERVICE_PRINCIPAL})

    def __init__(self, config: BaseModel, credential: bytes | None) -> None:
        super().__init__(config, credential)
        self._token: tuple[str, float] | None = None

    @property
    def cfg(self) -> AzureConfig:
        return self.config  # type: ignore[return-value]

    def _access_token(self) -> str:
        if self._token and self._token[1] > time.time() + 60:
            return self._token[0]
        response = http.send(
            "POST",
            f"{LOGIN}/{self.cfg.tenant_id}/oauth2/v2.0/token",
            form={
                "grant_type": "client_credentials",
                "client_id": self.cfg.client_id,
                "client_secret": (self._credential or b"").decode().strip(),
                "scope": f"{ARM}/.default",
            },
        )
        if response.status == 400 or response.status == 401:
            raise http.ProviderAuthError(f"Azure sign-in failed: {_azure_error(response)}", status=response.status)
        http.expect_ok(response, "Azure sign-in", message_of=_azure_error)
        body = response.json()
        self._token = (str(body["access_token"]), time.time() + int(body.get("expires_in", 3600)))
        return self._token[0]

    def _get(self, url: str, what: str) -> Any:
        response = http.send("GET", url, headers={"Authorization": f"Bearer {self._access_token()}"})
        http.expect_ok(response, f"Azure {what}", message_of=_azure_error)
        return response.json()

    def _managed_request(self, method: str, url: str, what: str, body: dict[str, Any] | None = None) -> Any:
        response = http.send(method, url, headers={"Authorization": f"Bearer {self._access_token()}"}, json_body=body)
        http.expect_ok(response, f"Azure {what}", message_of=_azure_error)
        return response.json() if response.body else {}, response.headers

    def _wait_managed_cluster(self, resource_url: str, *, gone: bool = False) -> dict[str, Any] | None:
        deadline = time.monotonic() + 3600
        while time.monotonic() < deadline:
            response = http.send("GET", resource_url, headers={"Authorization": f"Bearer {self._access_token()}"})
            if gone and response.status == 404:
                return None
            http.expect_ok(response, "Azure read managed cluster", message_of=_azure_error)
            resource = response.json()
            state = (resource.get("properties") or {}).get("provisioningState")
            if state == "Succeeded":
                return resource
            if state in {"Failed", "Canceled", "Cancelled"}:
                raise http.ProviderApiError(f"AKS entered {state} state")
            time.sleep(15)
        raise http.ProviderApiError("Timed out waiting for AKS managed cluster")

    def managed_cluster_id(self, name: str, options: dict[str, object]) -> str:
        resource_group = str(options.get("managed_resource_group") or self.cfg.resource_group or "")
        if not resource_group:
            raise http.ProviderApiError("AKS needs a managed resource group")
        return f"{self.cfg.subscription_id}/{resource_group}/{name}"

    def create_managed_cluster(self, request) -> Any:
        from app.modules.providers.types import ManagedClusterResult

        options = request.options
        resource_group = str(options.get("managed_resource_group") or self.cfg.resource_group or "")
        location = str(options.get("managed_location") or "")
        subnet_id = str(options.get("managed_subnet_id") or "")
        vm_size = str(options.get("managed_node_vm_size") or "Standard_D2s_v5")
        if not resource_group or not location:
            raise http.ProviderApiError("AKS requires options.managed_resource_group and options.managed_location")
        if not subnet_id:
            raise http.ProviderApiError("AKS requires options.managed_subnet_id for the node pool")
        cluster_name = request.name
        resource_path = (
            f"{ARM}/subscriptions/{self.cfg.subscription_id}/resourceGroups/{quote(resource_group, safe='')}"
            f"/providers/Microsoft.ContainerService/managedClusters/{quote(cluster_name, safe='')}"
        )
        base = f"{resource_path}?api-version={API_MANAGED_K8S}"
        resource_url = base
        existing_response = http.send("GET", resource_url, headers={"Authorization": f"Bearer {self._access_token()}"})
        if existing_response.status == 404:
            agent_pools = [
                {
                    "name": str(pool.get("name") or "workers")[:12].lower().replace("-", "") or "workers",
                    "count": int(pool.get("count") or 0),
                    "vmSize": vm_size,
                    "osType": "Linux",
                    "mode": "System" if index == 0 else "User",
                    "type": "VirtualMachineScaleSets",
                    "vnetSubnetID": subnet_id,
                    "enableAutoScaling": False,
                    "nodeLabels": {str(key): str(value) for key, value in (pool.get("labels") or {}).items()},
                    "nodeTaints": [
                        f"{item['key']}{'=' + str(item['value']) if item.get('value') else ''}:{item['effect']}"
                        for item in (pool.get("taints") or [])
                    ],
                }
                for index, pool in enumerate(request.node_pools)
                if int(pool.get("count") or 0) > 0
            ]
            if not agent_pools:
                raise http.ProviderApiError("AKS needs at least one initial worker node")
            body = {
                "location": location,
                "tags": {"kubernetes-platform:managed": "true"},
                "identity": {"type": "SystemAssigned"},
                "properties": {
                    "kubernetesVersion": request.version.removeprefix("v"),
                    "dnsPrefix": cluster_name,
                    "disableLocalAccounts": False,
                    "agentPoolProfiles": agent_pools,
                    "networkProfile": {
                        "networkPlugin": "azure",
                        "networkPluginMode": "overlay",
                        "podCidr": str(options.get("managed_pod_cidr") or "10.244.0.0/16"),
                        "serviceCidr": str(options.get("managed_service_cidr") or "10.96.0.0/12"),
                        "dnsServiceIP": str(options.get("managed_dns_service_ip") or "10.96.0.10"),
                        "loadBalancerSku": "standard",
                        "outboundType": "loadBalancer",
                    },
                },
            }
            _, headers = self._managed_request("PUT", resource_url, "create AKS cluster", body)
            poll_url = headers.get("azure-asyncoperation") or headers.get("location")
            if poll_url:
                deadline = time.monotonic() + 3600
                while time.monotonic() < deadline:
                    poll = http.send("GET", poll_url, headers={"Authorization": f"Bearer {self._access_token()}"})
                    http.expect_ok(poll, "Azure AKS operation", message_of=_azure_error)
                    status = str((poll.json() or {}).get("status", ""))
                    if status.lower() == "succeeded":
                        break
                    if status.lower() in {"failed", "canceled", "cancelled"}:
                        raise http.ProviderApiError(f"AKS deployment failed: {_azure_error(poll)}")
                    time.sleep(15)
                else:
                    raise http.ProviderApiError("Timed out waiting for AKS deployment operation")
        else:
            http.expect_ok(existing_response, "Azure read managed cluster", message_of=_azure_error)
            existing = existing_response.json()
            if (existing.get("tags") or {}).get("kubernetes-platform:managed") != "true":
                raise http.ProviderApiError("Refusing to use an AKS cluster without the platform ownership tag")
        self._wait_managed_cluster(resource_url)
        credentials_url = base.split("?", 1)[0] + "/listClusterAdminCredential?api-version=" + API_MANAGED_K8S
        credentials, _ = self._managed_request("POST", credentials_url, "read AKS cluster credentials", {})
        kubeconfig_doc = yaml.safe_load(base64.b64decode(credentials["kubeconfigs"][0]["value"]))
        server = kubeconfig_doc["clusters"][0]["cluster"]["server"]
        user = kubeconfig_doc["users"][0]["user"]
        ca = kubeconfig_doc["clusters"][0]["cluster"].get("certificate-authority-data")
        if not server or not ca or not user.get("client-certificate-data") or not user.get("client-key-data"):
            raise http.ProviderApiError("AKS did not return client certificate credentials for platform access")
        return ManagedClusterResult(
            external_id=f"{self.cfg.subscription_id}/{resource_group}/{cluster_name}",
            endpoint=server,
            certificate_authority_data=ca,
            client_certificate_data=user["client-certificate-data"],
            client_key_data=user["client-key-data"],
        )

    def delete_managed_cluster(self, external_id: str) -> None:
        subscription_id, separator, rest = external_id.partition("/")
        resource_group, separator2, name = rest.partition("/")
        if not separator or not separator2 or subscription_id != self.cfg.subscription_id:
            raise http.ProviderApiError("AKS ID must contain subscription/resource-group/name for this connection")
        resource_path = (
            f"{ARM}/subscriptions/{subscription_id}/resourceGroups/{quote(resource_group, safe='')}"
            f"/providers/Microsoft.ContainerService/managedClusters/{quote(name, safe='')}"
        )
        url = f"{resource_path}?api-version={API_MANAGED_K8S}"
        response = http.send("GET", url, headers={"Authorization": f"Bearer {self._access_token()}"})
        if response.status == 404:
            return
        http.expect_ok(response, "Azure read managed cluster", message_of=_azure_error)
        if (response.json().get("tags") or {}).get("kubernetes-platform:managed") != "true":
            raise http.ProviderApiError("Refusing to delete an AKS cluster without the platform ownership tag")
        self._managed_request("DELETE", url, "delete AKS cluster")
        self._wait_managed_cluster(url, gone=True)

    def _list(self, url: str, what: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        next_url: str | None = url
        for _ in range(MAX_PAGES):
            if not next_url:
                break
            page = self._get(next_url, what)
            items.extend(page.get("value", []))
            next_url = page.get("nextLink")
        return items

    def _scope(self) -> str:
        base = f"{ARM}/subscriptions/{self.cfg.subscription_id}"
        return f"{base}/resourceGroups/{self.cfg.resource_group}" if self.cfg.resource_group else base

    def validate_connection(self) -> ConnectionCheckResult:
        subscription = self._get(
            f"{ARM}/subscriptions/{self.cfg.subscription_id}?api-version={API_SUBSCRIPTIONS}", "read subscription"
        )
        details = {
            "subscription": str(subscription.get("displayName", "")),
            "state": str(subscription.get("state", "")),
            "tenant": self.cfg.tenant_id,
        }
        if details["state"] != "Enabled":
            return ConnectionCheckResult(ok=False, message=f"Subscription is {details['state']}", details=details)
        self._get(f"{self._scope()}/providers/Microsoft.Compute/virtualMachines?api-version={API_COMPUTE}", "list VMs")
        return ConnectionCheckResult(ok=True, message=f"Subscription {details['subscription']}", details=details)

    def discover(self) -> list[DiscoveredMachine]:
        vms = self._list(
            f"{self._scope()}/providers/Microsoft.Compute/virtualMachines?api-version={API_COMPUTE}&statusOnly=true",
            "list VMs",
        )
        nics = self._list(
            f"{self._scope()}/providers/Microsoft.Network/networkInterfaces?api-version={API_NETWORK}",
            "list network interfaces",
        )
        nic_ips = {
            str(nic.get("id", "")).lower(): [
                str(c.get("properties", {}).get("privateIPAddress"))
                for c in nic.get("properties", {}).get("ipConfigurations", [])
                if c.get("properties", {}).get("privateIPAddress")
            ]
            for nic in nics
        }
        sizes: dict[str, dict[str, tuple[int, int]]] = {}
        for location in {str(vm.get("location", "")) for vm in vms if vm.get("location")}:
            listed = self._list(
                f"{ARM}/subscriptions/{self.cfg.subscription_id}/providers/Microsoft.Compute/locations/"
                f"{location}/vmSizes?api-version={API_COMPUTE}",
                f"list VM sizes ({location})",
            )
            sizes[location] = {
                str(s["name"]): (int(s.get("numberOfCores", 0)), int(s.get("memoryInMB", 0))) for s in listed
            }

        machines = []
        for vm in vms:
            props = vm.get("properties", {})
            size = str(props.get("hardwareProfile", {}).get("vmSize", ""))
            cores, memory = sizes.get(str(vm.get("location", "")), {}).get(size, (0, 0))
            statuses = props.get("instanceView", {}).get("statuses", [])
            power = next(
                (str(s["code"]).split("/", 1)[1] for s in statuses if str(s.get("code", "")).startswith("PowerState/")),
                "unknown",
            )
            addresses = [
                MachineAddress(type="internal", address=ip)
                for ref in props.get("networkProfile", {}).get("networkInterfaces", [])
                for ip in nic_ips.get(str(ref.get("id", "")).lower(), [])
            ]
            resource_id = str(vm.get("id", ""))
            machines.append(
                DiscoveredMachine(
                    external_id=resource_id,
                    name=str(vm.get("name", "")),
                    addresses=addresses,
                    power_state={"running": "running", "deallocated": "stopped", "stopped": "stopped"}.get(
                        power, power
                    ),
                    hardware=HardwareSpec(cpu_cores=cores or None, memory_mb=memory or None),
                    os=OSInfo(family=str(props.get("storageProfile", {}).get("osDisk", {}).get("osType", "")) or None),
                    provider_metadata={
                        "location": str(vm.get("location", "")),
                        "size": size,
                        "resource_group": resource_id.split("/resourceGroups/")[-1].split("/")[0]
                        if "/resourceGroups/" in resource_id
                        else "",
                    },
                    importable=False,
                )
            )
        return machines

    def _vm_url(self, resource_group: str, name: str) -> str:
        return (
            f"{ARM}/subscriptions/{self.cfg.subscription_id}/resourceGroups/{quote(resource_group, safe='')}/"
            f"providers/Microsoft.Compute/virtualMachines/{quote(name, safe='')}?api-version={API_COMPUTE}"
        )

    def _vm(self, resource_group: str, name: str, *, instance_view: bool = False) -> dict[str, Any] | None:
        url = self._vm_url(resource_group, name)
        if instance_view:
            url += "&$expand=instanceView"
        response = http.send("GET", url, headers={"Authorization": f"Bearer {self._access_token()}"})
        if response.status == 404:
            return None
        http.expect_ok(response, "Azure read VM", message_of=_azure_error)
        return response.json()

    def _send(self, method: str, url: str, what: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        response = http.send(
            method,
            url,
            headers={"Authorization": f"Bearer {self._access_token()}"},
            json_body=body,
        )
        http.expect_ok(response, f"Azure {what}", message_of=_azure_error)
        return response.json() if response.body else {}

    def _addresses(self, vm: dict[str, Any]) -> list[MachineAddress]:
        addresses: list[MachineAddress] = []
        for ref in vm.get("properties", {}).get("networkProfile", {}).get("networkInterfaces", []):
            nic = self._get(f"{ref['id']}?api-version={API_NETWORK}", "read VM network interface")
            for config in nic.get("properties", {}).get("ipConfigurations", []):
                props = config.get("properties", {})
                if props.get("privateIPAddress"):
                    addresses.append(MachineAddress(type="internal", address=str(props["privateIPAddress"])))
                public_id = props.get("publicIPAddress", {}).get("id")
                if public_id:
                    public_ip = self._get(f"{public_id}?api-version={API_PUBLIC_IP}", "read public IP")
                    address = public_ip.get("properties", {}).get("ipAddress")
                    if address:
                        addresses.append(MachineAddress(type="external", address=str(address)))
        return addresses

    def _machine(self, resource_group: str, vm: dict[str, Any]) -> DiscoveredMachine:
        props = vm.get("properties", {})
        statuses = props.get("instanceView", {}).get("statuses", [])
        power = next(
            (str(s["code"]).split("/", 1)[1] for s in statuses if str(s.get("code", "")).startswith("PowerState/")),
            "unknown",
        )
        return DiscoveredMachine(
            external_id=str(vm.get("id", "")),
            name=str(vm.get("name", "")),
            addresses=self._addresses(vm),
            power_state={"running": "running", "deallocated": "stopped", "stopped": "stopped"}.get(power, power),
            provider_metadata={
                "location": str(vm.get("location", "")),
                "size": str(props.get("hardwareProfile", {}).get("vmSize", "")),
                "resource_group": resource_group,
            },
            importable=False,
        )

    def provision(self, request: MachineSpecRequest) -> DiscoveredMachine:
        options = request.provider_options
        resource_group = str(options.get("resource_group") or self.cfg.resource_group or "")
        location = str(options.get("location") or "")
        vm_size = str(options.get("vm_size") or "")
        nic_id = str(options.get("network_interface_id") or request.network or "")
        image = str(request.image or options.get("image") or "")
        image_parts = image.split(":")
        if not resource_group or not location or not vm_size or not nic_id or len(image_parts) != 4:
            raise http.ProviderApiError(
                "Azure provisioning requires resource_group, location, vm_size, network_interface_id and "
                "publisher:offer:sku:version image"
            )
        if not request.ssh_public_key:
            raise http.ProviderApiError("Azure provisioning requires an SSH public key")
        existing = self._vm(resource_group, request.name, instance_view=True)
        if existing is not None:
            if (existing.get("tags") or {}).get("kubernetes-platform-managed") != "true":
                raise http.ProviderApiError(f"An unmanaged Azure VM named '{request.name}' already exists")
            return self._machine(resource_group, existing)
        publisher, offer, sku, version = image_parts
        body = {
            "location": location,
            "tags": {"kubernetes-platform-managed": "true"},
            "properties": {
                "hardwareProfile": {"vmSize": vm_size},
                "storageProfile": {
                    "imageReference": {"publisher": publisher, "offer": offer, "sku": sku, "version": version},
                    "osDisk": {"createOption": "FromImage", "diskSizeGB": request.disk_gb, "deleteOption": "Delete"},
                },
                "osProfile": {
                    "computerName": request.name[:64],
                    "adminUsername": request.ssh_username,
                    "linuxConfiguration": {
                        "disablePasswordAuthentication": True,
                        "ssh": {
                            "publicKeys": [
                                {
                                    "path": f"/home/{request.ssh_username}/.ssh/authorized_keys",
                                    "keyData": request.ssh_public_key,
                                }
                            ]
                        },
                    },
                },
                "networkProfile": {"networkInterfaces": [{"id": nic_id, "properties": {"primary": True}}]},
            },
        }
        self._send("PUT", self._vm_url(resource_group, request.name), "create VM", body)
        vm = self._vm(resource_group, request.name, instance_view=True)
        if vm:
            return self._machine(resource_group, vm)
        return DiscoveredMachine(
            external_id=(
                f"{ARM}/subscriptions/{self.cfg.subscription_id}/resourceGroups/{resource_group}/"
                f"providers/Microsoft.Compute/virtualMachines/{request.name}"
            ),
            name=request.name,
            power_state="pending",
            provider_metadata={"location": location, "resource_group": resource_group},
            importable=False,
        )

    def _machine_id(self, external_id: str) -> tuple[str, str]:
        parts = external_id.split("/")
        try:
            return parts[parts.index("resourceGroups") + 1], parts[parts.index("virtualMachines") + 1]
        except (ValueError, IndexError):
            raise http.ProviderApiError("Azure machine ID must be a virtual machine resource ID") from None

    def get_status(self, machine: MachineRef) -> MachineStatus:
        resource_group, name = self._machine_id(machine.external_id)
        vm = self._vm(resource_group, name, instance_view=True)
        found = self._machine(resource_group, vm) if vm else None
        return MachineStatus(
            external_id=machine.external_id,
            exists=found is not None,
            power_state=found.power_state if found else "deleted",
            addresses=found.addresses if found else [],
        )

    def delete(self, machine: MachineRef) -> None:
        resource_group, name = self._machine_id(machine.external_id)
        vm = self._vm(resource_group, name)
        if vm is None:
            return
        if (vm.get("tags") or {}).get("kubernetes-platform-managed") != "true":
            raise http.ProviderApiError("Refusing to delete an Azure VM without the platform ownership tag")
        self._send("DELETE", self._vm_url(resource_group, name), "delete VM")
