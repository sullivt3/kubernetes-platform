"use client";

import { EmptyState, PageHeader, TableSkeleton } from "@/components/common/page";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ClusterScope, LiveError } from "@/features/kubernetes/shared";
import { KIND_CONFIG } from "@/features/workloads/kinds";
import { ResourceTable } from "@/features/workloads/resource-list";
import { usePodSecurity } from "@/lib/api/kube";

const modes = ["enforce", "audit", "warn"] as const;
const admissionKinds = ["validatingadmissionpolicies", "validatingadmissionpolicybindings"] as const;

function PodSecurity({ clusterId }: { clusterId: string }) {
  const query = usePodSecurity(clusterId);
  const rows = query.data?.items ?? [];
  return (
    <div className="space-y-3">
      <p className="text-sm text-muted-foreground">
        These are the labels set on each namespace. When Pod Security Admission is enabled, the control plane
        uses its defaults for unset labels. Defaults and exemptions are outside this view.
      </p>
      {query.isLoading && <TableSkeleton rows={4} columns={4} />}
      {query.isError && <LiveError error={query.error} clusterId={clusterId} onRetry={() => query.refetch()} />}
      {query.data && !rows.length && <EmptyState title="No namespaces" description="The cluster returned no namespaces." />}
      {query.data?.truncated && <p role="status" className="text-sm text-muted-foreground">The namespace list was truncated.</p>}
      {rows.length > 0 && (
        <Table>
          <TableHeader><TableRow>
            <TableHead>Namespace</TableHead>
            {modes.map((mode) => <TableHead key={mode} className="capitalize">{mode}</TableHead>)}
          </TableRow></TableHeader>
          <TableBody>
            {rows.map(({ namespace, modes: labels }) => (
              <TableRow key={namespace}>
                <TableCell className="font-medium">{namespace}</TableCell>
                {modes.map((mode) => (
                  <TableCell key={mode}>
                    <span className={labels[mode].level === null ? "text-muted-foreground" : "font-mono"}>
                      {labels[mode].level ?? "No level label"}
                    </span>
                    {labels[mode].version !== null && <p className="font-mono text-xs text-muted-foreground">{labels[mode].version}</p>}
                  </TableCell>
                ))}
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </div>
  );
}

export function SecurityPoliciesPage() {
  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="Security"
        title="Security policies"
        description="Inspect Pod Security Admission labels and Kubernetes validating admission configuration."
      />
      <ClusterScope>
        {(clusterId) => (
          <Tabs defaultValue="pod-security">
            <TabsList className="flex-wrap">
              <TabsTrigger value="pod-security">Pod security</TabsTrigger>
              <TabsTrigger value={admissionKinds[0]}>Admission policies</TabsTrigger>
              <TabsTrigger value={admissionKinds[1]}>Policy bindings</TabsTrigger>
            </TabsList>
            <TabsContent value="pod-security" className="pt-3"><PodSecurity clusterId={clusterId} /></TabsContent>
            {admissionKinds.map((kind) => (
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
