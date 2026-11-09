"use client";

import { Info } from "lucide-react";

import { PageHeader } from "@/components/common/page";
import { ProviderInventory } from "./connections";

export function VirtualMachinesPage() {
  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow="Infrastructure"
        title="Virtual machines"
        description="Machines on virtualization and local providers, straight from each provider. Import existing ones into the inventory, or provision new ones."
      />
      <ProviderInventory
        categories={["virtualization", "local"]}
        emptyHint="Add a Proxmox VE or Local (Docker) connection to see its virtual machines here."
      />
    </div>
  );
}

export function CloudResourcesPage() {
  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow="Infrastructure"
        title="Cloud resources"
        description="Inventory AWS, Azure and Google Cloud instances; provision machines through AWS and Google Cloud connections."
      />
      <div className="flex gap-3 rounded-lg border border-sky-500/20 bg-sky-500/5 p-4 text-sm">
        <Info className="mt-0.5 size-4 shrink-0 text-sky-400" />
        <p className="text-muted-foreground">
          Cloud providers are in preview until verified against live accounts. AWS and Google Cloud support
          creating and deleting platform-owned machines with explicit image and network settings. Existing
          discovered instances remain read-only. Managed Kubernetes (EKS, AKS, GKE) remains planned.
        </p>
      </div>
      <ProviderInventory
        categories={["cloud"]}
        emptyHint="Add an AWS, Azure or Google Cloud connection on the Providers page to inventory its instances."
      />
    </div>
  );
}
