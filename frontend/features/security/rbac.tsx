"use client";

import { PageHeader } from "@/components/common/page";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ClusterScope } from "@/features/kubernetes/shared";
import { KIND_CONFIG } from "@/features/workloads/kinds";
import { ResourceTable } from "@/features/workloads/resource-list";

const rbacKinds = ["roles", "clusterroles", "rolebindings", "clusterrolebindings"] as const;

export function RbacPage() {
  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="Security"
        title="Roles & bindings"
        description="Inspect Kubernetes permission rules and the bindings that grant them in the selected cluster."
      />
      <ClusterScope>
        {(clusterId) => (
          <Tabs defaultValue="roles">
            <TabsList className="flex-wrap">
              {rbacKinds.map((kind) => <TabsTrigger key={kind} value={kind}>{KIND_CONFIG[kind].title}</TabsTrigger>)}
            </TabsList>
            {rbacKinds.map((kind) => (
              <TabsContent key={kind} value={kind} className="space-y-3 pt-3">
                <p className="text-sm text-muted-foreground">{KIND_CONFIG[kind].description}</p>
                <ResourceTable key={clusterId} clusterId={clusterId} kind={kind} />
              </TabsContent>
            ))}
          </Tabs>
        )}
      </ClusterScope>
    </div>
  );
}
