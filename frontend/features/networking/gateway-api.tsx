"use client";

import { FileCode } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { PageHeader } from "@/components/common/page";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ClusterScope } from "@/features/kubernetes/shared";
import { ResourceTable } from "@/features/workloads/resource-list";
import { useClusters } from "@/lib/api/hooks";
import type { ResourceKind } from "@/lib/api/workloads";

const resources: { kind: ResourceKind; label: string }[] = [
  { kind: "gatewayclasses", label: "Classes" },
  { kind: "gateways", label: "Gateways" },
  { kind: "httproutes", label: "HTTPRoutes" },
  { kind: "referencegrants", label: "ReferenceGrants" },
];

function GatewayResources({ clusterId }: { clusterId: string }) {
  const [kind, setKind] = useState<ResourceKind>("gatewayclasses");
  const name = useClusters().data?.find((cluster) => cluster.id === clusterId)?.name;
  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="Networking"
        title={name ? `Gateway API · ${name}` : "Gateway API"}
        description="Inspect GatewayClasses, Gateways, HTTPRoutes and ReferenceGrants. The Gateway API CRDs and a compatible controller must be installed in the cluster."
        actions={<Button asChild size="sm" variant="outline"><Link href="/kubernetes/apply"><FileCode /> Apply YAML</Link></Button>}
      />
      <Tabs value={kind} onValueChange={(value) => setKind(value as ResourceKind)}>
        <TabsList className="flex-wrap">
          {resources.map((resource) => <TabsTrigger key={resource.kind} value={resource.kind}>{resource.label}</TabsTrigger>)}
        </TabsList>
        {resources.map((resource) => (
          <TabsContent key={resource.kind} value={resource.kind} className="pt-3">
            <ResourceTable clusterId={clusterId} kind={resource.kind} />
          </TabsContent>
        ))}
      </Tabs>
    </div>
  );
}

export function GatewayApiPage() {
  return <ClusterScope>{(clusterId) => <GatewayResources clusterId={clusterId} />}</ClusterScope>;
}
