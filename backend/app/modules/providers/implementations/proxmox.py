"""Proxmox VE provider: VM lifecycle through the Proxmox REST API (api2/json).

Provisioning clones a cloud-init template:

  find-or-clone (full clone, marked as platform-managed) -> configure CPU, memory,
  cloud-init user + SSH key, network -> grow the boot disk -> start

Every step is idempotent so the orchestration engine can retry or resume it: a
retried provision finds the VM it created by name *and* management marker; VMs
without the marker are never adopted, changed or deleted.

Requirements on the Proxmox side (documented for operators):
  * an API token (``user@realm!tokenid``) with VM.Allocate, VM.Clone, VM.Config.*,
    VM.PowerMgmt, VM.Audit, Datastore.AllocateSpace and Sys.Audit on the relevant paths;
  * a VM template with a cloud-init drive and the QEMU guest agent installed and
    enabled (the platform reads the VM's IP address through the agent).

Status: PREVIEW — implemented against the documented Proxmox VE 8 API and tested
with an API simulator, not yet verified against a live server.
"""

from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import quote

from pydantic import AnyHttpUrl, BaseModel, Field, field_validator

from app.modules.credentials.models import CredentialKind
from app.modules.providers import http
from app.modules.providers.base import InfrastructureProvider
from app.modules.providers.http import ProviderApiError, TlsPolicy
from app.modules.providers.types import (
    ConnectionCheckResult,
    DiscoveredMachine,
    DiskInfo,
    HardwareSpec,
    ImplementationStatus,
    MachineAddress,
    MachineRef,
    MachineSpecRequest,
    MachineStatus,
    NetworkConfigRequest,
    PowerAction,
    ProviderCapability,
    ProviderCategory,
    ProviderType,
)

MANAGED_MARKER = "managed-by: kubernetes-platform"
MANAGED_TAG = "kplatform"
TASK_TIMEOUT_SECONDS = 600
DISK_KEY = re.compile(r"^(scsi|virtio|sata|ide)\d+$")
SIZE_RE = re.compile(r"size=(\d+(?:\.\d+)?)([KMGT]?)")


class ProxmoxConfig(BaseModel):
    api_url: AnyHttpUrl = Field(description="e.g. https://pve.example.internal:8006")
    token_id: str = Field(
        pattern=r"^[^@\s]+@[^!\s]+![A-Za-z0-9][A-Za-z0-9_.-]*$",
        description="API token ID, e.g. platform@pve!kplatform (the secret goes in the credential)",
    )
    node: str = Field(min_length=1, max_length=64, description="Proxmox node that holds the template and new VMs")
    template_vmid: int = Field(ge=100, description="VMID of the cloud-init template to clone")
    default_storage: str | None = Field(
        default=None, description="Storage for cloned disks (default: same as template)"
    )
    default_bridge: str | None = Field(default=None, description="Bridge for the VM NIC (default: template's)")
    ca_certificate: str | None = Field(default=None, description="PEM CA bundle for the API certificate")
    tls_fingerprint: str | None = Field(
        default=None,
        pattern=r"^([0-9A-Fa-f]{2}:?){31}[0-9A-Fa-f]{2}$",
        description="Pin the API certificate by SHA-256 fingerprint (as shown in the Proxmox UI)",
    )

    @field_validator("api_url")
    @classmethod
    def https_only(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        if value.scheme != "https":
            raise ValueError("The Proxmox API must be reached over https")
        return value


def _parse_size_gb(spec: str) -> float:
    match = SIZE_RE.search(spec)
    if not match:
        return 0.0
    number, unit = float(match.group(1)), match.group(2) or "G"
    return number * {"K": 1 / 1024 / 1024, "M": 1 / 1024, "G": 1, "T": 1024}[unit]


def _split_ref(external_id: str) -> tuple[str, int]:
    node, _, vmid = external_id.partition("/")
    if not node or not vmid.isdigit():
        raise ProviderApiError(f"Invalid Proxmox machine reference '{external_id}' (expected node/vmid)")
    return node, int(vmid)


def _boot_disk(config: dict[str, Any]) -> str | None:
    order = str(config.get("boot", ""))
    if order.startswith("order="):
        for device in order[len("order=") :].split(";"):
            if DISK_KEY.match(device) and "media=cdrom" not in str(config.get(device, "")):
                return device
    for key in sorted(config):
        value = str(config[key])
        if DISK_KEY.match(key) and "cloudinit" not in value and "media=cdrom" not in value:
            return key
    return None


class ProxmoxProvider(InfrastructureProvider):
    provider_type = ProviderType.PROXMOX
    display_name = "Proxmox VE"
    description = "Provision and manage virtual machines on Proxmox Virtual Environment (cloud-init templates)."
    category = ProviderCategory.VIRTUALIZATION
    capabilities = frozenset(
        {
            ProviderCapability.VALIDATE_CONNECTION,
            ProviderCapability.DISCOVER,
            ProviderCapability.PROVISION,
            ProviderCapability.DELETE,
            ProviderCapability.STATUS,
            ProviderCapability.SPECIFICATIONS,
            ProviderCapability.POWER,
            ProviderCapability.NETWORK,
        }
    )
    implementation_status = ImplementationStatus.PREVIEW
    config_model = ProxmoxConfig
    credential_kinds = frozenset({CredentialKind.PROXMOX_API_TOKEN})

    # Seams for tests (fast polling).
    poll_interval_seconds = 2.0

    @property
    def cfg(self) -> ProxmoxConfig:
        return self.config  # type: ignore[return-value]

    # ---- API plumbing -------------------------------------------------------------

    def _tls(self) -> TlsPolicy:
        return TlsPolicy(ca_pem=self.cfg.ca_certificate, fingerprint_sha256=self.cfg.tls_fingerprint)

    def _url(self, path: str) -> str:
        return f"{str(self.cfg.api_url).rstrip('/')}/api2/json{path}"

    @staticmethod
    def _message(response: http.HttpResponse) -> str:
        try:
            body = response.json() or {}
        except ValueError:
            return response.text[:300]
        parts = [str(body.get("message") or "").strip()]
        if isinstance(body.get("errors"), dict):
            parts.extend(f"{k}: {v}" for k, v in body["errors"].items())
        return "; ".join(p for p in parts if p)

    def _api(self, method: str, path: str, *, params: dict[str, Any] | None = None, what: str = "") -> Any:
        secret = (self._credential or b"").decode().strip()
        headers = {"Authorization": f"PVEAPIToken={self.cfg.token_id}={secret}"}
        if method in ("GET", "DELETE"):
            response = http.send(method, self._url(path), headers=headers, params=params, tls=self._tls())
        else:
            response = http.send(method, self._url(path), headers=headers, form=params or {}, tls=self._tls())
        http.expect_ok(response, what or f"Proxmox {method} {path}", message_of=self._message)
        return (response.json() or {}).get("data")

    def _wait_task(self, node: str, upid: Any, what: str) -> None:
        if not isinstance(upid, str) or not upid.startswith("UPID:"):
            return  # synchronous call
        deadline = time.monotonic() + TASK_TIMEOUT_SECONDS
        path = f"/nodes/{node}/tasks/{quote(upid, safe='')}/status"
        while True:
            status = self._api("GET", path, what=f"{what} (task status)") or {}
            if status.get("status") == "stopped":
                exit_status = str(status.get("exitstatus", ""))
                if exit_status != "OK" and not exit_status.startswith("WARNINGS"):
                    raise ProviderApiError(f"{what} failed: {exit_status or 'unknown error'}")
                return
            if time.monotonic() > deadline:
                raise ProviderApiError(f"{what} did not finish within {TASK_TIMEOUT_SECONDS}s")
            time.sleep(self.poll_interval_seconds)

    def _vms(self) -> list[dict[str, Any]]:
        resources = self._api("GET", "/cluster/resources", params={"type": "vm"}, what="List VMs") or []
        return [r for r in resources if r.get("type") == "qemu"]

    def _config(self, node: str, vmid: int) -> dict[str, Any]:
        return self._api("GET", f"/nodes/{node}/qemu/{vmid}/config", what=f"Read VM {vmid} config") or {}

    def _is_managed(self, config: dict[str, Any]) -> bool:
        return MANAGED_MARKER in str(config.get("description", ""))

    def _find_managed(self, name: str) -> tuple[str, int] | None:
        for vm in self._vms():
            if vm.get("name") == name and not vm.get("template"):
                node, vmid = str(vm["node"]), int(vm["vmid"])
                if not self._is_managed(self._config(node, vmid)):
                    raise ProviderApiError(
                        f"A VM named '{name}' already exists (VMID {vmid}) and is not managed by the platform"
                    )
                return node, vmid
        return None

    def _addresses(self, node: str, vmid: int) -> list[MachineAddress]:
        try:
            data = self._api("GET", f"/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces", what="Guest agent")
        except ProviderApiError:
            return []  # agent not running yet / not installed
        addresses: list[MachineAddress] = []
        for interface in (data or {}).get("result", []):
            if interface.get("name") == "lo":
                continue
            for entry in interface.get("ip-addresses", []):
                ip = str(entry.get("ip-address", ""))
                if entry.get("ip-address-type") == "ipv4" and not ip.startswith(("127.", "169.254.")):
                    addresses.append(MachineAddress(type="internal", address=ip))
        return addresses

    def _describe(self, vm: dict[str, Any], *, with_addresses: bool) -> DiscoveredMachine:
        node, vmid = str(vm["node"]), int(vm["vmid"])
        running = vm.get("status") == "running"
        return DiscoveredMachine(
            external_id=f"{node}/{vmid}",
            name=str(vm.get("name") or f"vm-{vmid}"),
            addresses=self._addresses(node, vmid) if with_addresses and running else [],
            power_state="running" if running else "stopped",
            hardware=HardwareSpec(
                cpu_cores=int(vm["maxcpu"]) if vm.get("maxcpu") else None,
                memory_mb=int(vm["maxmem"]) // (1024 * 1024) if vm.get("maxmem") else None,
                disks=[DiskInfo(name="total", size_gb=round(int(vm["maxdisk"]) / 1024**3, 1))]
                if vm.get("maxdisk")
                else [],
            ),
            provider_metadata={"node": node, "vmid": str(vmid), "tags": str(vm.get("tags", ""))},
        )

    # ---- capabilities -------------------------------------------------------------

    def validate_connection(self) -> ConnectionCheckResult:
        version = self._api("GET", "/version", what="Read Proxmox version") or {}
        nodes = {n.get("node"): n for n in self._api("GET", "/nodes", what="List nodes") or []}
        details = {"version": str(version.get("version", "")), "nodes": ", ".join(sorted(str(n) for n in nodes))}
        node = nodes.get(self.cfg.node)
        if node is None:
            return ConnectionCheckResult(ok=False, message=f"Node '{self.cfg.node}' not found", details=details)
        if node.get("status") != "online":
            return ConnectionCheckResult(
                ok=False, message=f"Node '{self.cfg.node}' is {node.get('status')}", details=details
            )
        try:
            template = self._config(self.cfg.node, self.cfg.template_vmid)
        except ProviderApiError as exc:
            return ConnectionCheckResult(
                ok=False, message=f"Template VM {self.cfg.template_vmid} not readable: {exc.message}", details=details
            )
        problems = []
        if not template.get("template"):
            problems.append(f"VM {self.cfg.template_vmid} is not a template")
        if not any("cloudinit" in str(v) for v in template.values()):
            problems.append("the template has no cloud-init drive")
        if not str(template.get("agent", "0")).startswith(("1", "enabled=1")):
            problems.append("the QEMU guest agent is not enabled on the template")
        details["template"] = str(template.get("name", self.cfg.template_vmid))
        if problems:
            return ConnectionCheckResult(
                ok=False, message="Template not usable: " + "; ".join(problems), details=details
            )
        return ConnectionCheckResult(
            ok=True, message=f"Proxmox VE {details['version']}, node {self.cfg.node} online", details=details
        )

    def discover(self) -> list[DiscoveredMachine]:
        return [self._describe(vm, with_addresses=True) for vm in self._vms() if not vm.get("template")]

    def provision(self, request: MachineSpecRequest) -> DiscoveredMachine:
        if not request.ssh_public_key:
            raise ProviderApiError("A public SSH key is required to provision a Proxmox VM")
        found = self._find_managed(request.name)
        if found is None:
            node = self.cfg.node
            vmid = int(self._api("GET", "/cluster/nextid", what="Allocate VMID"))
            clone_params: dict[str, Any] = {
                "newid": vmid,
                "name": request.name,
                "full": True,
                "description": MANAGED_MARKER,
                "storage": self.cfg.default_storage,
            }
            upid = self._api(
                "POST", f"/nodes/{node}/qemu/{self.cfg.template_vmid}/clone", params=clone_params, what="Clone template"
            )
            self._wait_task(node, upid, f"Clone of template {self.cfg.template_vmid}")
        else:
            node, vmid = found

        status = self._api("GET", f"/nodes/{node}/qemu/{vmid}/status/current", what="Read VM status") or {}
        if status.get("status") != "running":
            config = self._config(node, vmid)
            ipconfig = str(request.provider_options.get("ipconfig") or "ip=dhcp")
            settings: dict[str, Any] = {
                "cores": request.cpu_cores,
                "memory": request.memory_mb,
                "ciuser": request.ssh_username,
                # Proxmox expects the sshkeys value itself URL-encoded (documented quirk).
                "sshkeys": quote(request.ssh_public_key.strip() + "\n", safe=""),
                "ipconfig0": ipconfig,
                "agent": "1",
                "tags": MANAGED_TAG,
            }
            bridge = request.network or self.cfg.default_bridge
            if bridge:
                settings["net0"] = f"virtio,bridge={bridge}"
            self._api("PUT", f"/nodes/{node}/qemu/{vmid}/config", params=settings, what="Configure VM")
            disk = _boot_disk(config)
            if disk and _parse_size_gb(str(config.get(disk, ""))) < request.disk_gb:
                upid = self._api(
                    "PUT",
                    f"/nodes/{node}/qemu/{vmid}/resize",
                    params={"disk": disk, "size": f"{request.disk_gb}G"},
                    what="Resize disk",
                )
                self._wait_task(node, upid, "Disk resize")
            upid = self._api("POST", f"/nodes/{node}/qemu/{vmid}/status/start", what="Start VM")
            self._wait_task(node, upid, "VM start")
        return DiscoveredMachine(
            external_id=f"{node}/{vmid}",
            name=request.name,
            power_state="running",
            addresses=self._addresses(node, vmid),
            provider_metadata={"node": node, "vmid": str(vmid)},
        )

    def delete(self, machine: MachineRef) -> None:
        node, vmid = _split_ref(machine.external_id)
        try:
            config = self._config(node, vmid)
        except ProviderApiError as exc:
            if exc.status in (404, 500) and "does not exist" in exc.message:
                return  # already gone
            raise
        if not self._is_managed(config):
            raise ProviderApiError(f"VM {vmid} is not managed by the platform; refusing to delete it")
        status = self._api("GET", f"/nodes/{node}/qemu/{vmid}/status/current", what="Read VM status") or {}
        if status.get("status") == "running":
            upid = self._api("POST", f"/nodes/{node}/qemu/{vmid}/status/stop", what="Stop VM")
            self._wait_task(node, upid, "VM stop")
        upid = self._api(
            "DELETE",
            f"/nodes/{node}/qemu/{vmid}",
            params={"purge": 1, "destroy-unreferenced-disks": 1},
            what="Delete VM",
        )
        self._wait_task(node, upid, "VM deletion")

    def get_status(self, machine: MachineRef) -> MachineStatus:
        node, vmid = _split_ref(machine.external_id)
        try:
            status = self._api("GET", f"/nodes/{node}/qemu/{vmid}/status/current", what="Read VM status") or {}
        except ProviderApiError as exc:
            if "does not exist" in exc.message:
                return MachineStatus(external_id=machine.external_id, exists=False, power_state="unknown")
            raise
        running = status.get("status") == "running"
        return MachineStatus(
            external_id=machine.external_id,
            exists=True,
            power_state="running" if running else "stopped",
            addresses=self._addresses(node, vmid) if running else [],
        )

    def get_specifications(self, machine: MachineRef) -> HardwareSpec:
        node, vmid = _split_ref(machine.external_id)
        config = self._config(node, vmid)
        disks = [
            DiskInfo(name=key, size_gb=round(_parse_size_gb(str(value)), 1), type=str(value).split(":")[0])
            for key, value in sorted(config.items())
            if DISK_KEY.match(key) and "cloudinit" not in str(value) and "media=cdrom" not in str(value)
        ]
        return HardwareSpec(
            cpu_cores=int(config.get("cores", 1)) * int(config.get("sockets", 1)),
            memory_mb=int(config.get("memory", 0)) or None,
            architecture="aarch64" if config.get("arch") == "aarch64" else "x86_64",
            disks=disks,
        )

    def set_power_state(self, machine: MachineRef, action: PowerAction) -> MachineStatus:
        node, vmid = _split_ref(machine.external_id)
        if action is PowerAction.STOP:
            # Graceful ACPI/agent shutdown, forced after the timeout.
            upid = self._api(
                "POST",
                f"/nodes/{node}/qemu/{vmid}/status/shutdown",
                params={"timeout": 120, "forceStop": True},
                what="Shut down VM",
            )
        else:
            upid = self._api("POST", f"/nodes/{node}/qemu/{vmid}/status/{action.value}", what=f"{action.value} VM")
        self._wait_task(node, upid, f"VM {action.value}")
        return self.get_status(machine)

    def configure_network(self, machine: MachineRef, request: NetworkConfigRequest) -> None:
        node, vmid = _split_ref(machine.external_id)
        settings: dict[str, Any] = {"net0": f"virtio,bridge={request.network}"}
        if request.addresses:
            ip = f"ip={request.addresses[0]}"
            gateway = request.options.get("gateway")
            settings["ipconfig0"] = f"{ip},gw={gateway}" if gateway else ip
        self._api("PUT", f"/nodes/{node}/qemu/{vmid}/config", params=settings, what="Configure VM network")
