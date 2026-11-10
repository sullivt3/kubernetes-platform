"use client";

import { useMutation } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { apiRequest } from "@/lib/api/client";
import type { Machine, OperationDetail } from "@/lib/api/types";

export function MachineNetwork({ machine, onClose }: { machine: Machine; onClose: () => void }) {
  const router = useRouter();
  const [network, setNetwork] = useState("");
  const [addresses, setAddresses] = useState("");
  const [groups, setGroups] = useState("");
  const [gateway, setGateway] = useState("");
  const [confirm, setConfirm] = useState("");
  const aws = machine.provider_type === "aws";
  const azure = machine.provider_type === "azure";
  const gcp = machine.provider_type === "gcp";
  const proxmox = machine.provider_type === "proxmox";
  const update = useMutation({
    mutationFn: () => apiRequest<OperationDetail>(`/machines/${machine.id}/network`, {
      method: "POST", query: { confirm }, body: {
        network: network.trim(), addresses: addresses.split(",").map((v) => v.trim()).filter(Boolean),
        options: { ...(aws ? { security_group_ids: groups.split(",").map((v) => v.trim()).filter(Boolean) } : {}), ...(gateway ? { gateway: gateway.trim() } : {}) },
      },
    }),
    onSuccess: (operation) => { onClose(); router.push(`/operations/${operation.id}`); },
    onError: (error) => toast.error("Could not configure network", { description: error.message }),
  });
  return <Dialog open onOpenChange={(open) => !open && onClose()}><DialogContent>
    <DialogHeader><DialogTitle>Network: {machine.name}</DialogTitle><DialogDescription>
      {aws ? "Change security groups within the VM's existing subnet." : azure ? "Change the subnet and private IP of a VM with one network interface. The interface uses DHCP when the private IP is empty." : gcp ? "Configure alias IP CIDR ranges on an existing interface. The primary IP and VPC stay in place." : proxmox ? "Change the bridge and cloud-init address on net0. Guest changes can require a reboot." : "Move the VM's single network adapter to another port group. Guest addressing uses DHCP."}
      {" "}This may interrupt SSH and Kubernetes traffic.
    </DialogDescription></DialogHeader>
    <div className="space-y-4">
      <div className="space-y-1"><Label>{aws ? "Existing subnet ID" : azure ? "Subnet resource ID" : gcp ? "Interface name" : proxmox ? "Bridge" : "Port group"}</Label><Input value={network} onChange={(e) => setNetwork(e.target.value)} placeholder={gcp ? "nic0" : undefined} /></div>
      {aws && <div className="space-y-1"><Label>Security group IDs (comma separated)</Label><Input value={groups} onChange={(e) => setGroups(e.target.value)} /></div>}
      {(azure || gcp || proxmox) && <div className="space-y-1"><Label>{gcp ? "Alias CIDR ranges (comma separated; empty removes aliases)" : proxmox ? "Address with prefix (optional, e.g. 10.0.0.20/24)" : "Private IP (optional)"}</Label><Input value={addresses} onChange={(e) => setAddresses(e.target.value)} /></div>}
      {proxmox && <div className="space-y-1"><Label>Gateway (optional)</Label><Input value={gateway} onChange={(e) => setGateway(e.target.value)} /></div>}
      <div className="space-y-1"><Label>Type {machine.name} to confirm</Label><Input value={confirm} onChange={(e) => setConfirm(e.target.value)} autoComplete="off" /></div>
    </div>
    <DialogFooter><Button variant="outline" onClick={onClose}>Cancel</Button><Button disabled={confirm !== machine.name || !network.trim() || (aws && !groups.trim()) || update.isPending} onClick={() => update.mutate()}>{update.isPending ? "Submitting…" : "Apply network change"}</Button></DialogFooter>
  </DialogContent></Dialog>;
}
