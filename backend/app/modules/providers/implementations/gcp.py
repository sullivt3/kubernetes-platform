"""Google Cloud provider — foundations: service account validation and read-only
Compute Engine inventory. Creating and managing instances is planned for Phase 5.

Authentication follows Google's service-account flow: a JWT signed (RS256) with
the key's private key is exchanged for an access token. No Google SDK is required.
The service account needs ``roles/compute.viewer`` on the project.

Status: PREVIEW — tested against recorded API responses, not yet verified against a
live project.
"""

from __future__ import annotations

import base64
import json
import time
from typing import Any

from pydantic import BaseModel, Field

from app.modules.credentials.models import CredentialKind
from app.modules.providers import http
from app.modules.providers.base import InfrastructureProvider
from app.modules.providers.http import ProviderApiError
from app.modules.providers.types import (
    ConnectionCheckResult,
    DiscoveredMachine,
    HardwareSpec,
    ImplementationStatus,
    MachineAddress,
    ProviderCapability,
    ProviderCategory,
    ProviderType,
)

COMPUTE = "https://compute.googleapis.com/compute/v1"
SCOPE = "https://www.googleapis.com/auth/compute.readonly"
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


class GcpProvider(InfrastructureProvider):
    provider_type = ProviderType.GCP
    display_name = "Google Cloud"
    description = "Compute Engine inventory and project validation today; instance provisioning and GKE are planned."
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
    config_model = GcpConfig
    credential_kinds = frozenset({CredentialKind.GCP_SERVICE_ACCOUNT})

    def __init__(self, config: BaseModel, credential: bytes | None) -> None:
        super().__init__(config, credential)
        self._token: tuple[str, float] | None = None

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

    def _access_token(self) -> str:
        if self._token and self._token[1] > time.time() + 60:
            return self._token[0]
        key = self._key()
        response = http.send(
            "POST",
            key.get("token_uri") or DEFAULT_TOKEN_URI,
            form={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": service_account_jwt(key, scope=SCOPE),
            },
        )
        if response.status in (400, 401):
            raise http.ProviderAuthError(f"Google sign-in failed: {_gcp_error(response)}", status=response.status)
        http.expect_ok(response, "Google sign-in", message_of=_gcp_error)
        body = response.json()
        self._token = (str(body["access_token"]), time.time() + int(body.get("expires_in", 3600)))
        return self._token[0]

    def _get(self, url: str, what: str, params: dict[str, Any] | None = None) -> Any:
        response = http.send("GET", url, headers={"Authorization": f"Bearer {self._access_token()}"}, params=params)
        http.expect_ok(response, f"Google Cloud {what}", message_of=_gcp_error)
        return response.json()

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
                    external_id=f"{zone}/{instance.get('id')}",
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
