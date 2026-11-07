"""Microsoft Azure provider — foundations: service principal validation and read-only
virtual machine inventory. Creating and managing VMs is planned for Phase 5.

Authentication uses the OAuth 2.0 client-credentials flow against Microsoft Entra ID
and the Azure Resource Manager REST API; no Azure SDK is required. The service
principal needs the built-in *Reader* role on the subscription.

Status: PREVIEW — tested against recorded API responses, not yet verified against a
live subscription.
"""

from __future__ import annotations

import time
from typing import Any

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
    description = "Virtual machine inventory and subscription validation today; VM provisioning and AKS are planned."
    category = ProviderCategory.CLOUD
    capabilities = frozenset({ProviderCapability.VALIDATE_CONNECTION, ProviderCapability.DISCOVER})
    planned_capabilities = frozenset(
        {
            ProviderCapability.PROVISION,
            ProviderCapability.DELETE,
            ProviderCapability.STATUS,
            ProviderCapability.POWER,
            ProviderCapability.NETWORK,
        }
    )
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
