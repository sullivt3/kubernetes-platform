"use client";

import { useState } from "react";

import { PageHeader } from "@/components/common/page";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { ClusterScope } from "@/features/kubernetes/shared";
import { useClusters } from "@/lib/api/hooks";
import { TEMPLATES } from "./templates";
import { YamlApplyPanel } from "./yaml-apply";

function ApplyBody({ clusterId }: { clusterId: string }) {
  const name = useClusters().data?.find((c) => c.id === clusterId)?.name;
  const [template, setTemplate] = useState("Deployment");
  return (
    <>
      <PageHeader
        eyebrow="Kubernetes"
        title={name ? `Apply YAML · ${name}` : "Apply YAML"}
        description="Paste, import or write manifests (multiple documents allowed). A server-side dry run validates every object and shows the exact diff before anything changes; apply uses server-side apply and is audited."
        actions={
          <Select value={template} onValueChange={setTemplate}>
            <SelectTrigger className="w-48" aria-label="Start from template"><SelectValue /></SelectTrigger>
            <SelectContent>
              {Object.keys(TEMPLATES).map((t) => <SelectItem key={t} value={t}>Template: {t}</SelectItem>)}
            </SelectContent>
          </Select>
        }
      />
      <YamlApplyPanel key={`${clusterId}-${template}`} clusterId={clusterId} initial={TEMPLATES[template]} />
    </>
  );
}

export function ApplyYamlPage() {
  return (
    <div className="space-y-6">
      <ClusterScope>{(id) => <ApplyBody clusterId={id} />}</ClusterScope>
    </div>
  );
}
