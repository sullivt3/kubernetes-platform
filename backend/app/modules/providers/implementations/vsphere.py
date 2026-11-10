"""VMware vSphere provider: VM lifecycle via vCenter."""

from __future__ import annotations

import base64
import ssl
import time
from typing import Any

import yaml
from pydantic import BaseModel, Field

from app.modules.credentials.models import CredentialKind
from app.modules.providers.base import InfrastructureProvider
from app.modules.providers.http import ProviderApiError, ProviderUnreachableError
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

OWNER = "guestinfo.kubernetes-platform.managed"


class VsphereConfig(BaseModel):
    server: str = Field(min_length=1, description="vCenter hostname")
    username: str = Field(min_length=1, description="vCenter login, for example administrator@vsphere.local")
    datacenter: str = Field(min_length=1)
    cluster: str | None = None
    datastore: str | None = None
    network: str | None = None
    template: str | None = Field(
        default=None, description="Linux template with VMware Tools and cloud-init VMware datasource"
    )
    verify_tls: bool = True


class VsphereProvider(InfrastructureProvider):
    provider_type = ProviderType.VSPHERE
    display_name = "VMware vSphere"
    description = "Provision and manage virtual machines through VMware vCenter."
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
    config_model = VsphereConfig
    credential_kinds = frozenset({CredentialKind.VSPHERE_PASSWORD})

    def __init__(self, config: BaseModel, credential: bytes | None) -> None:
        super().__init__(config, credential)
        self._session: Any = None

    @property
    def cfg(self) -> VsphereConfig:
        return self.config  # type: ignore[return-value]

    def _content(self) -> Any:
        if self._session is None:
            from pyVim.connect import SmartConnect

            context = ssl.create_default_context()
            if not self.cfg.verify_tls:
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            try:
                self._session = SmartConnect(
                    host=self.cfg.server,
                    user=self.cfg.username,
                    pwd=(self._credential or b"").decode(),
                    sslContext=context,
                    httpConnectionTimeout=120,
                )
            except OSError as exc:
                raise ProviderUnreachableError(f"Cannot reach vCenter: {exc}") from exc
            except Exception as exc:
                raise ProviderApiError(f"vCenter authentication failed: {exc}") from exc
        return self._session.RetrieveContent()

    def _objects(self, root: Any, types: list[Any]) -> list[Any]:
        view = self._content().viewManager.CreateContainerView(root, types, True)
        try:
            return list(view.view)
        finally:
            view.Destroy()

    def _find(self, root: Any, kind: Any, name: str) -> Any:
        found = [obj for obj in self._objects(root, [kind]) if obj.name == name]
        if len(found) != 1:
            raise ProviderApiError(f"Expected one {kind.__name__} named '{name}', found {len(found)}")
        return found[0]

    def _dc(self) -> Any:
        from pyVmomi import vim

        return self._find(self._content().rootFolder, vim.Datacenter, self.cfg.datacenter)

    def _vm(self, external_id: str) -> Any:
        return self._content().searchIndex.FindByUuid(self._dc(), external_id, True, True)

    @staticmethod
    def _owned(vm: Any) -> None:
        if not vm or {option.key: option.value for option in vm.config.extraConfig}.get(OWNER) != "true":
            raise ProviderApiError("VM is missing or does not have the platform ownership marker")

    @staticmethod
    def _wait(task: Any) -> Any:
        deadline = time.monotonic() + 1800
        while time.monotonic() < deadline:
            state = str(task.info.state)
            if state == "success":
                return task.info.result
            if state == "error":
                raise ProviderApiError(f"vCenter task failed: {task.info.error.msg}")
            time.sleep(2)
        raise ProviderApiError("vCenter task timed out; inspect the task before retrying")

    @staticmethod
    def _hardware(vm: Any) -> HardwareSpec:
        from pyVmomi import vim

        return HardwareSpec(
            cpu_cores=vm.config.hardware.numCPU,
            memory_mb=vm.config.hardware.memoryMB,
            disks=[
                DiskInfo(
                    name=device.deviceInfo.label if device.deviceInfo else str(device.key),
                    size_gb=device.capacityInKB / 1024 / 1024,
                    type="virtual-disk",
                )
                for device in vm.config.hardware.device
                if isinstance(device, vim.vm.device.VirtualDisk)
            ],
        )

    def _describe(self, vm: Any) -> DiscoveredMachine:
        import ipaddress

        addresses = []
        for nic in vm.guest.net or []:
            for value in nic.ipAddress or []:
                try:
                    ip = ipaddress.ip_address(value)
                except ValueError:
                    continue
                if not (ip.is_loopback or ip.is_link_local):
                    addresses.append(MachineAddress(type="internal" if ip.is_private else "external", address=str(ip)))
        return DiscoveredMachine(
            external_id=vm.config.instanceUuid,
            name=vm.name,
            power_state={"poweredOn": "running", "poweredOff": "stopped", "suspended": "suspended"}.get(
                str(vm.runtime.powerState), "unknown"
            ),
            addresses=addresses,
            hardware=self._hardware(vm),
            provider_metadata={"datacenter": self.cfg.datacenter, "guest_id": vm.config.guestId, "vm_moid": vm._moId},
        )

    def validate_connection(self) -> ConnectionCheckResult:
        content = self._content()
        dc = self._dc()
        return ConnectionCheckResult(
            ok=True,
            message=f"{content.about.fullName}: {dc.name}",
            details={"version": content.about.version, "datacenter": dc.name},
        )

    def discover(self) -> list[DiscoveredMachine]:
        from pyVmomi import vim

        return [
            self._describe(vm)
            for vm in self._objects(self._dc().vmFolder, [vim.VirtualMachine])
            if not vm.config.template
        ]

    @staticmethod
    def _backing(network: Any) -> Any:
        from pyVmomi import vim

        if isinstance(network, vim.dvs.DistributedVirtualPortgroup):
            switch = network.config.distributedVirtualSwitch
            if switch is None:
                raise ProviderApiError("The distributed port group has no backing switch")
            return vim.vm.device.VirtualEthernetCard.DistributedVirtualPortBackingInfo(
                port=vim.dvs.PortConnection(switchUuid=switch.uuid, portgroupKey=network.key)
            )
        return vim.vm.device.VirtualEthernetCard.NetworkBackingInfo(network=network, deviceName=network.name)

    def provision(self, request: MachineSpecRequest) -> DiscoveredMachine:
        from pyVmomi import vim

        dc = self._dc()
        options = request.provider_options
        template_name = str(request.image or self.cfg.template or "")
        cluster_name = str(options.get("cluster") or self.cfg.cluster or "")
        datastore_name = str(options.get("datastore") or self.cfg.datastore or "")
        network_name = str(options.get("network") or request.network or self.cfg.network or "")
        if not all((template_name, cluster_name, datastore_name, network_name, request.ssh_public_key)):
            raise ProviderApiError(
                "vSphere provisioning requires template, cluster, datastore, network and an SSH public key"
            )
        existing = [vm for vm in self._objects(dc.vmFolder, [vim.VirtualMachine]) if vm.name == request.name]
        if existing:
            if len(existing) != 1:
                raise ProviderApiError("Multiple VMs share the requested name")
            self._owned(existing[0])
            return self._describe(existing[0])
        template = self._find(dc.vmFolder, vim.VirtualMachine, template_name)
        if not template.config.template:
            raise ProviderApiError("The selected image must be a VM template")
        cluster = self._find(dc.hostFolder, vim.ClusterComputeResource, cluster_name)
        datastore = self._find(dc.datastoreFolder, vim.Datastore, datastore_name)
        network = self._find(dc.networkFolder, vim.Network, network_name)
        disks = [device for device in template.config.hardware.device if isinstance(device, vim.vm.device.VirtualDisk)]
        nics = [
            device
            for device in template.config.hardware.device
            if isinstance(device, vim.vm.device.VirtualEthernetCard)
        ]
        if len(disks) != 1 or len(nics) != 1:
            raise ProviderApiError(
                "Provisioning currently requires a template with exactly one disk and one network adapter"
            )
        disk = disks[0]
        if request.disk_gb * 1024 * 1024 < disk.capacityInKB:
            raise ProviderApiError("The requested disk cannot be smaller than the template disk")
        disk.capacityInKB = request.disk_gb * 1024 * 1024
        nic = nics[0]
        nic.backing = self._backing(network)
        metadata = {
            "instance-id": request.name,
            "local-hostname": request.name,
            "network": {"version": 2, "ethernets": {"primary": {"match": {"name": "e*"}, "dhcp4": True}}},
        }
        userdata = "#cloud-config\n" + yaml.safe_dump(
            {
                "users": [
                    {
                        "name": request.ssh_username,
                        "shell": "/bin/bash",
                        "sudo": "ALL=(ALL) NOPASSWD:ALL",
                        "lock_passwd": True,
                        "ssh_authorized_keys": [request.ssh_public_key],
                    }
                ],
                "ssh_pwauth": False,
                "growpart": {"mode": "auto", "devices": ["/"]},
                "resize_rootfs": True,
            }
        )
        extra = {
            OWNER: "true",
            "guestinfo.metadata": base64.b64encode(yaml.safe_dump(metadata).encode()).decode(),
            "guestinfo.metadata.encoding": "base64",
            "guestinfo.userdata": base64.b64encode(userdata.encode()).decode(),
            "guestinfo.userdata.encoding": "base64",
        }
        config = vim.vm.ConfigSpec(
            numCPUs=request.cpu_cores,
            memoryMB=request.memory_mb,
            extraConfig=[vim.option.OptionValue(key=key, value=value) for key, value in extra.items()],
            deviceChange=[vim.vm.device.VirtualDeviceSpec(operation="edit", device=device) for device in (disk, nic)],
        )
        clone = vim.vm.CloneSpec(
            location=vim.vm.RelocateSpec(datastore=datastore, pool=cluster.resourcePool),
            config=config,
            powerOn=True,
            template=False,
        )
        created = self._wait(template.Clone(folder=dc.vmFolder, name=request.name, spec=clone))
        return self._describe(created)

    def get_status(self, machine: MachineRef) -> MachineStatus:
        vm = self._vm(machine.external_id)
        if vm is None:
            return MachineStatus(external_id=machine.external_id, exists=False, power_state="deleted")
        found = self._describe(vm)
        return MachineStatus(
            external_id=machine.external_id, exists=True, power_state=found.power_state, addresses=found.addresses
        )

    def get_specifications(self, machine: MachineRef) -> HardwareSpec:
        vm = self._vm(machine.external_id)
        if vm is None:
            raise ProviderApiError("VM does not exist", status=404)
        return self._hardware(vm)

    def delete(self, machine: MachineRef) -> None:
        vm = self._vm(machine.external_id)
        if vm is None:
            return
        self._owned(vm)
        if str(vm.runtime.powerState) != "poweredOff":
            self._wait(vm.PowerOffVM_Task())
        self._wait(vm.Destroy_Task())

    def set_power_state(self, machine: MachineRef, action: PowerAction) -> MachineStatus:
        vm = self._vm(machine.external_id)
        self._owned(vm)
        state = str(vm.runtime.powerState)
        if action is PowerAction.START and state != "poweredOn":
            self._wait(vm.PowerOnVM_Task())
        elif action is PowerAction.STOP and state != "poweredOff":
            self._wait(vm.PowerOffVM_Task())
        elif action is PowerAction.REBOOT:
            if state != "poweredOn":
                raise ProviderApiError("A VM must be powered on before rebooting")
            vm.RebootGuest()
        return self.get_status(machine)

    def configure_network(self, machine: MachineRef, request: NetworkConfigRequest) -> None:
        from pyVmomi import vim

        vm = self._vm(machine.external_id)
        self._owned(vm)
        if request.addresses:
            raise ProviderApiError(
                "vSphere port-group changes use guest DHCP; static guest address configuration is unsupported"
            )
        nics = [d for d in vm.config.hardware.device if isinstance(d, vim.vm.device.VirtualEthernetCard)]
        if len(nics) != 1:
            raise ProviderApiError("Port-group changes currently require exactly one network adapter")
        nics[0].backing = self._backing(self._find(self._dc().networkFolder, vim.Network, request.network))
        self._wait(
            vm.ReconfigVM_Task(
                spec=vim.vm.ConfigSpec(deviceChange=[vim.vm.device.VirtualDeviceSpec(operation="edit", device=nics[0])])
            )
        )

    def close(self) -> None:
        if self._session is not None:
            from pyVim.connect import Disconnect

            Disconnect(self._session)
            self._session = None
