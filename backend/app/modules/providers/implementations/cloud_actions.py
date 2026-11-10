"""Cloud VM power and narrowly scoped network changes, guarded by ownership."""

import ipaddress
from typing import Any
from urllib.parse import quote

from app.modules.providers.http import ProviderApiError
from app.modules.providers.types import MachineRef, MachineStatus, NetworkConfigRequest, PowerAction


class CloudMachineActions:
    """Uses each provider's existing authenticated request and inventory methods."""

    def _owned(self: Any, machine: MachineRef) -> tuple[str, Any]:
        kind = self.provider_type.value
        if kind == "aws":
            from app.modules.providers.implementations.aws import _text

            region, sep, name = machine.external_id.partition("/")
            if not sep or region not in [self.cfg.region, *self.cfg.discovery_regions] or not name.startswith("i-"):
                raise ProviderApiError("Machine ID must name an EC2 instance in the configured regions")
            instance = self._instance(region, name)
            if instance is None:
                raise ProviderApiError("EC2 instance does not exist", status=404)
            tags = {_text(t, "key"): _text(t, "value") for t in instance.iterfind("tagSet/item")}
            if tags.get("kubernetes-platform:managed") != "true":
                raise ProviderApiError("Refusing to modify an EC2 instance without the platform ownership tag")
            return region, instance
        if kind == "azure":
            group, name = self._machine_id(machine.external_id)
            instance = self._vm(group, name)
            if instance is None:
                raise ProviderApiError("Azure VM does not exist", status=404)
            if (instance.get("tags") or {}).get("kubernetes-platform-managed") != "true":
                raise ProviderApiError("Refusing to modify an Azure VM without the platform ownership tag")
            return self._vm_url(group, name).split("?", 1)[0], instance
        zone, sep, name = machine.external_id.partition("/")
        if not sep or not zone or not name or "/" in name or (self.cfg.zones and zone not in self.cfg.zones):
            raise ProviderApiError("Machine ID must identify zone/name within this connection")
        instance = self._instance(zone, name)
        if instance is None:
            raise ProviderApiError("Compute Engine instance does not exist", status=404)
        if (instance.get("labels") or {}).get("kplatform_managed") != "true":
            raise ProviderApiError("Refusing to modify an instance without the platform ownership label")
        return (
            f"https://compute.googleapis.com/compute/v1/projects/{self.project}/zones/{quote(zone, safe='')}/"
            f"instances/{quote(name, safe='')}",
            instance,
        )

    def set_power_state(self: Any, machine: MachineRef, action: PowerAction) -> MachineStatus:
        base, _ = self._owned(machine)
        kind = self.provider_type.value
        if kind == "aws":
            operation = {
                PowerAction.START: "StartInstances",
                PowerAction.STOP: "StopInstances",
                PowerAction.REBOOT: "RebootInstances",
            }[action]
            self._query(
                "ec2",
                base,
                {"Action": operation, "Version": "2016-11-15", "InstanceId.1": machine.external_id.split("/", 1)[1]},
                what=operation,
            )
        elif kind == "azure":
            from app.modules.providers.implementations.azure import API_COMPUTE

            operation = {PowerAction.START: "start", PowerAction.STOP: "deallocate", PowerAction.REBOOT: "restart"}[
                action
            ]
            self._send("POST", f"{base}/{operation}?api-version={API_COMPUTE}", operation + " VM", {})
        else:
            operation = {PowerAction.START: "start", PowerAction.STOP: "stop", PowerAction.REBOOT: "reset"}[action]
            self._request("POST", f"{base}/{operation}", operation + " instance", {})
        return self.get_status(machine)

    def configure_network(self: Any, machine: MachineRef, request: NetworkConfigRequest) -> None:
        base, instance = self._owned(machine)
        kind = self.provider_type.value
        if kind == "aws":
            from app.modules.providers.implementations.aws import _text

            if request.addresses or request.network != _text(instance, "subnetId"):
                raise ProviderApiError(
                    "EC2 supports security group changes within the existing subnet; "
                    "address changes and subnet migration are unsupported"
                )
            groups = request.options.get("security_group_ids")
            if (
                not isinstance(groups, list)
                or not groups
                or any(not isinstance(g, str) or not g.startswith("sg-") for g in groups)
            ):
                raise ProviderApiError("Provide a nonempty security_group_ids list")
            form = {
                "Action": "ModifyInstanceAttribute",
                "Version": "2016-11-15",
                "InstanceId": machine.external_id.split("/", 1)[1],
            }
            form.update({f"GroupId.{index}": str(group) for index, group in enumerate(groups, 1)})
            self._query("ec2", base, form, what="ModifyInstanceAttribute")
        elif kind == "azure":
            self._configure_azure_nic(instance, request)
        else:
            nic = next((n for n in instance.get("networkInterfaces", []) if n.get("name") == request.network), None)
            if nic is None:
                raise ProviderApiError("network must name an attached interface, for example nic0")
            try:
                ranges = [str(ipaddress.ip_network(value, strict=True)) for value in request.addresses]
            except ValueError as exc:
                raise ProviderApiError(f"Invalid alias IP range: {exc}") from exc
            body = {
                "name": nic["name"],
                "fingerprint": nic.get("fingerprint"),
                "aliasIpRanges": [{"ipCidrRange": value} for value in ranges],
            }
            self._request(
                "PATCH",
                f"{base}/updateNetworkInterface?networkInterface={quote(request.network, safe='')}",
                "configure interface alias ranges",
                body,
            )

    def _configure_azure_nic(self: Any, vm: dict[str, Any], request: NetworkConfigRequest) -> None:
        from app.modules.providers.implementations.azure import API_NETWORK, ARM

        refs = vm.get("properties", {}).get("networkProfile", {}).get("networkInterfaces", [])
        nic_id = request.options.get("network_interface_id") or (refs[0]["id"] if len(refs) == 1 else None)
        if not isinstance(nic_id, str) or nic_id not in [ref["id"] for ref in refs]:
            raise ProviderApiError("Select a network_interface_id attached to this VM")
        prefix = f"/subscriptions/{self.cfg.subscription_id}/"
        if not request.network.lower().startswith(prefix.lower()) or "/subnets/" not in request.network.lower():
            raise ProviderApiError("network must name an Azure subnet in this subscription")
        if len(request.addresses) > 1:
            raise ProviderApiError("Provide at most one private IP address")
        nic = self._get(f"{ARM}{nic_id}?api-version={API_NETWORK}", "read network interface")
        props = nic.get("properties", {})
        configs = props.get("ipConfigurations") or []
        if len(configs) != 1:
            raise ProviderApiError("This operation requires a NIC with exactly one IP configuration")
        config = configs[0]
        ip_props = {
            key: value
            for key, value in config["properties"].items()
            if key
            in {
                "primary",
                "privateIPAddress",
                "privateIPAllocationMethod",
                "privateIPAddressVersion",
                "publicIPAddress",
                "subnet",
            }
        }
        ip_props["subnet"] = {"id": request.network}
        if request.addresses:
            try:
                ip_props["privateIPAddress"] = str(ipaddress.ip_address(request.addresses[0]))
            except ValueError as exc:
                raise ProviderApiError(str(exc)) from exc
            ip_props["privateIPAllocationMethod"] = "Static"
        else:
            ip_props.pop("privateIPAddress", None)
            ip_props["privateIPAllocationMethod"] = "Dynamic"
        writable = {
            key: value
            for key, value in props.items()
            if key in {"dnsSettings", "enableIPForwarding", "enableAcceleratedNetworking", "networkSecurityGroup"}
        }
        writable["ipConfigurations"] = [{"name": config["name"], "properties": ip_props}]
        self._send(
            "PUT",
            f"{ARM}{nic_id}?api-version={API_NETWORK}",
            "configure network interface",
            {"location": nic["location"], "tags": nic.get("tags", {}), "properties": writable},
        )
