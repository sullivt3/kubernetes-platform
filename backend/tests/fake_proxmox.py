"""A small Proxmox VE API simulator (api2/json) used as the provider HTTP transport.

It models what the provider relies on: token auth, cluster resources, VMID
allocation, asynchronous tasks (UPIDs), clone/config/resize/start/stop/delete, the
guest agent's network report and Proxmox's error shapes.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

from app.modules.providers.http import HttpRequest, HttpResponse, TlsPolicy

TOKEN_ID = "platform@pve!kplatform"
TOKEN_SECRET = "11111111-2222-3333-4444-555555555555"


@dataclass
class Vm:
    vmid: int
    name: str
    node: str = "pve1"
    template: bool = False
    status: str = "stopped"
    config: dict[str, Any] = field(default_factory=dict)
    ip: str | None = None
    agent_ready: bool = True


class FakeProxmox:
    def __init__(self) -> None:
        self.vms: dict[int, Vm] = {
            9000: Vm(
                9000,
                "ubuntu-2404-cloud",
                template=True,
                config={
                    "name": "ubuntu-2404-cloud",
                    "template": 1,
                    "scsi0": "local-lvm:base-9000-disk-0,size=3584M",
                    "ide2": "local-lvm:vm-9000-cloudinit,media=cdrom",
                    "boot": "order=scsi0",
                    "net0": "virtio=BC:24:11:00:00:01,bridge=vmbr0",
                    "agent": "1",
                    "cores": 1,
                    "memory": 1024,
                },
            )
        }
        self.tasks: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str, dict[str, str]]] = []
        self.task_polls_before_done = 1
        self.fail_tasks: set[str] = set()  # task types that end with an error
        self.tls_seen: list[TlsPolicy] = []
        self.next_ip = 50

    # ---- helpers ------------------------------------------------------------------

    def _task(self, node: str, kind: str, vmid: int) -> str:
        upid = f"UPID:{node}:0000{len(self.tasks):04X}:00000000:6720A1B2:{kind}:{vmid}:{TOKEN_ID}:"
        self.tasks[upid] = {
            "polls": 0,
            "exitstatus": f"{kind} failed: simulated error" if kind in self.fail_tasks else "OK",
        }
        return upid

    @staticmethod
    def _ok(data: Any) -> HttpResponse:
        return HttpResponse(200, {"Content-Type": "application/json"}, json.dumps({"data": data}).encode())

    @staticmethod
    def _error(status: int, message: str, errors: dict[str, str] | None = None) -> HttpResponse:
        body: dict[str, Any] = {"data": None, "message": message}
        if errors:
            body["errors"] = errors
        return HttpResponse(status, {"Content-Type": "application/json"}, json.dumps(body).encode())

    def _vm(self, node: str, vmid: int) -> Vm | None:
        vm = self.vms.get(vmid)
        return vm if vm and vm.node == node else None

    def _missing(self, node: str, vmid: int) -> HttpResponse:
        return self._error(500, f"Configuration file 'nodes/{node}/qemu-server/{vmid}.conf' does not exist")

    # ---- transport ------------------------------------------------------------------

    def __call__(self, request: HttpRequest, tls: TlsPolicy) -> HttpResponse:
        self.tls_seen.append(tls)
        parts = urlsplit(request.url)
        path = parts.path.removeprefix("/api2/json")
        params = dict(parse_qsl(parts.query))
        if request.body:
            params.update(dict(parse_qsl(request.body.decode(), keep_blank_values=True)))
        self.calls.append((request.method, path, params))
        if request.headers.get("Authorization") != f"PVEAPIToken={TOKEN_ID}={TOKEN_SECRET}":
            return self._error(401, "authentication failure")
        return self._route(request.method, path, params)

    def _route(self, method: str, path: str, params: dict[str, str]) -> HttpResponse:
        if method == "GET" and path == "/version":
            return self._ok({"version": "8.3.2", "release": "8.3", "repoid": "3e76eec2"})
        if method == "GET" and path == "/nodes":
            return self._ok([{"node": "pve1", "status": "online"}, {"node": "pve2", "status": "offline"}])
        if method == "GET" and path == "/cluster/resources":
            return self._ok(
                [
                    {
                        "id": f"qemu/{vm.vmid}",
                        "type": "qemu",
                        "vmid": vm.vmid,
                        "name": vm.name,
                        "node": vm.node,
                        "status": vm.status,
                        "template": 1 if vm.template else 0,
                        "maxcpu": int(vm.config.get("cores", 1)),
                        "maxmem": int(vm.config.get("memory", 1024)) * 1024 * 1024,
                        "maxdisk": 10 * 1024**3,
                        "tags": vm.config.get("tags", ""),
                    }
                    for vm in self.vms.values()
                ]
                + [{"id": "lxc/200", "type": "lxc", "vmid": 200, "name": "ct", "node": "pve1", "status": "running"}]
            )
        if method == "GET" and path == "/cluster/nextid":
            return self._ok(str(max(v for v in self.vms if v < 9000) + 1 if any(v < 9000 for v in self.vms) else 100))

        task = re.fullmatch(r"/nodes/([^/]+)/tasks/([^/]+)/status", path)
        if task and method == "GET":
            upid = unquote(task.group(2))
            state = self.tasks.get(upid)
            if state is None:
                return self._error(500, "no such task")
            state["polls"] += 1
            if state["polls"] <= self.task_polls_before_done:
                return self._ok({"status": "running", "upid": upid})
            return self._ok({"status": "stopped", "exitstatus": state["exitstatus"], "upid": upid})

        match = re.fullmatch(r"/nodes/([^/]+)/qemu/(\d+)(/.*)?", path)
        if not match:
            return self._error(501, f"Method '{method} {path}' not implemented")
        node, vmid, rest = match.group(1), int(match.group(2)), match.group(3) or ""
        vm = self._vm(node, vmid)

        if rest == "/clone" and method == "POST":
            if vm is None:
                return self._missing(node, vmid)
            newid = int(params["newid"])
            if newid in self.vms:
                return self._error(500, f"unable to create VM {newid}: config file already exists")
            config = {k: v for k, v in vm.config.items() if k not in ("template", "name")}
            config["scsi0"] = config["scsi0"].replace("base-9000", f"vm-{newid}")
            config["name"] = params["name"]
            if params.get("description"):
                config["description"] = params["description"]
            self.vms[newid] = Vm(newid, params["name"], node=node, config=config)
            return self._ok(self._task(node, "qmclone", vmid))
        if vm is None:
            return self._missing(node, vmid)
        if rest == "/config" and method == "GET":
            return self._ok(dict(vm.config))
        if rest == "/config" and method == "PUT":
            for key in ("cores", "memory"):
                if key in params and not params[key].isdigit():
                    return self._error(400, "Parameter verification failed.", {key: "type check ('integer') failed"})
            if "sshkeys" in params and " " in params["sshkeys"]:
                return self._error(400, "Parameter verification failed.", {"sshkeys": "invalid urlencoded string"})
            vm.config.update(params)
            return self._ok(None)
        if rest == "/resize" and method == "PUT":
            size = params["size"]
            vm.config[params["disk"]] = re.sub(r"size=[^,]+", f"size={size}", vm.config[params["disk"]])
            return self._ok(self._task(node, "resize", vmid))
        if rest == "/status/current" and method == "GET":
            return self._ok({"status": vm.status, "vmid": vmid, "name": vm.name, "qmpstatus": vm.status})
        if rest.startswith("/status/") and method == "POST":
            action = rest.rsplit("/", 1)[1]
            if action == "start":
                vm.status = "running"
                vm.ip = vm.ip or f"192.168.10.{self.next_ip}"
                self.next_ip += 1
            elif action in ("stop", "shutdown"):
                vm.status = "stopped"
            return self._ok(self._task(node, f"qm{action}", vmid))
        if rest == "/agent/network-get-interfaces" and method == "GET":
            if vm.status != "running" or not vm.agent_ready:
                return self._error(500, "QEMU guest agent is not running")
            return self._ok(
                {
                    "result": [
                        {"name": "lo", "ip-addresses": [{"ip-address": "127.0.0.1", "ip-address-type": "ipv4"}]},
                        {
                            "name": "eth0",
                            "ip-addresses": [
                                {"ip-address": vm.ip, "ip-address-type": "ipv4", "prefix": 24},
                                {"ip-address": "fe80::1", "ip-address-type": "ipv6", "prefix": 64},
                            ],
                        },
                    ]
                }
            )
        if rest == "" and method == "DELETE":
            if vm.status == "running":
                return self._error(500, f"VM {vmid} is running - destroy failed")
            del self.vms[vmid]
            return self._ok(self._task(node, "qmdestroy", vmid))
        return self._error(501, f"Method '{method} {path}' not implemented")
