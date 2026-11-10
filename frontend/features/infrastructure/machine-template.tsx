"use client";

import type { ReactNode } from "react";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

export interface MachineSize {
  cpu: number;
  memory: number;
  disk: number;
  image?: string;
  options?: Record<string, string>;
}
export const DEFAULT_MACHINE_SIZE: MachineSize = { cpu: 2, memory: 2048, disk: 20 };
export const machineTemplate = (size: MachineSize) => ({
  cpu_cores: size.cpu, memory_mb: size.memory, disk_gb: size.disk,
  image: size.image?.trim() || null,
  provider_options: Object.fromEntries(Object.entries(size.options ?? {}).filter(([, v]) => v.trim()).map(([key, value]) => [key, key === "security_group_ids" ? value.split(",").map((v) => v.trim()).filter(Boolean) : value.trim()])),
});


function TemplateField({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return <div className="space-y-1.5"><Label>{label}</Label>{children}{hint && <p className="text-xs text-muted-foreground">{hint}</p>}</div>;
}

const VM_FIELDS: Record<string, [string, string][]> = {
  aws: [["instance_type", "EC2 instance type"], ["subnet_id", "Subnet ID"], ["security_group_ids", "Security group IDs (comma separated)"]],
  azure: [["resource_group", "Resource group"], ["location", "Azure location"], ["vm_size", "VM size"], ["subnet_id", "Subnet resource ID"]],
  gcp: [["zone", "Zone"], ["machine_type", "Machine type"], ["network", "VPC network URL"], ["subnetwork", "Subnetwork URL"]],
  vsphere: [["datastore", "Datastore"], ["network", "Port group"]],
};

export function MachineTemplateEditor({ size, onChange, provider, managed = false, local = false }: { size: MachineSize; onChange: (size: MachineSize) => void; provider?: string; managed?: boolean; local?: boolean }) {
  const num = (v: string) => Math.max(0, Number(v.replace(/\D/g, "")) || 0);
  return (
    <div className="space-y-3">
    <div className="grid grid-cols-3 gap-2">
      {!managed && !local && <><TemplateField label="CPU">
        <Input value={size.cpu} onChange={(e) => onChange({ ...size, cpu: num(e.target.value) })} inputMode="numeric" />
      </TemplateField>
      <TemplateField label="Memory (MB)">
        <Input value={size.memory} onChange={(e) => onChange({ ...size, memory: num(e.target.value) })} inputMode="numeric" />
      </TemplateField>
      </>}
      {!local && <TemplateField label="Disk (GB)">
        <Input value={size.disk} onChange={(e) => onChange({ ...size, disk: num(e.target.value) })} inputMode="numeric" />
      </TemplateField>}
    </div>
    {local && <p className="text-xs text-muted-foreground">Nodes share the Docker host&apos;s resources. Ensure Docker Desktop has enough CPU, memory and disk for this cluster.</p>}
    {managed && <TemplateField label="Machine type override (optional)" hint="Uses the cloud machine type selected in Infrastructure when empty."><Input value={size.options?.machine_type ?? ""} onChange={(e) => onChange({ ...size, options: { ...size.options, machine_type: e.target.value } })} /></TemplateField>}
    {!managed && !local && provider && VM_FIELDS[provider] && <>
      <p className="text-xs text-muted-foreground">The cloud machine type determines CPU and memory. The image must support SSH, cloud-init and passwordless sudo.</p>
      <TemplateField label={provider === "vsphere" ? "VM template" : "OS image"} hint={provider === "azure" ? "publisher:offer:sku:version" : provider === "aws" ? "AMI ID, for example ami-..." : provider === "gcp" ? "Compute Engine image resource URL" : "Cloud-init enabled template name"}>
        <Input value={size.image ?? ""} onChange={(e) => onChange({ ...size, image: e.target.value })} />
      </TemplateField>
      <div className="grid gap-3 md:grid-cols-2">{VM_FIELDS[provider].map(([key, label]) => <TemplateField key={key} label={label}><Input value={size.options?.[key] ?? ""} onChange={(e) => onChange({ ...size, options: { ...size.options, [key]: e.target.value } })} /></TemplateField>)}</div>
    </>}
    </div>
  );
}
