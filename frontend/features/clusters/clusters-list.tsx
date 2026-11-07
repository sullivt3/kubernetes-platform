"use client";

import { Boxes, Plus } from "lucide-react";
import Link from "next/link";

import { EmptyState, ErrorState, PageHeader, TableSkeleton } from "@/components/common/page";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { useClusters } from "@/lib/api/hooks";
import { formatRelative } from "@/lib/format";
import { ClusterStateBadge } from "./cluster-state";

export function ClustersList() {
  const clusters = useClusters();
  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow="Kubernetes"
        title="Clusters"
        description="Clusters created by the platform. Importing existing clusters arrives in Phase 1."
        actions={
          <Button asChild size="sm">
            <Link href="/kubernetes/create"><Plus /> Create cluster</Link>
          </Button>
        }
      />
      <Card className="py-0">
        <CardContent className="px-0">
          {clusters.isLoading && <div className="p-4"><TableSkeleton rows={3} columns={6} /></div>}
          {clusters.isError && <div className="p-4"><ErrorState error={clusters.error} onRetry={() => clusters.refetch()} /></div>}
          {clusters.data?.length === 0 && (
            <EmptyState
              className="m-4"
              icon={Boxes}
              title="No clusters yet"
              description="Register Linux machines, then let the platform install Kubernetes on them with kubeadm."
              action={
                <div className="flex gap-2">
                  <Button asChild variant="outline" size="sm"><Link href="/infrastructure/machines">Register machines</Link></Button>
                  <Button asChild size="sm"><Link href="/kubernetes/create"><Plus /> Create cluster</Link></Button>
                </div>
              }
            />
          )}
          {clusters.data && clusters.data.length > 0 && (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Cluster</TableHead>
                  <TableHead>State</TableHead>
                  <TableHead>Distribution</TableHead>
                  <TableHead>Version</TableHead>
                  <TableHead>Nodes</TableHead>
                  <TableHead>API endpoint</TableHead>
                  <TableHead>Created</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {clusters.data.map((c) => (
                  <TableRow key={c.id} className="group">
                    <TableCell>
                      <Link href={`/kubernetes/clusters/${c.id}`} className="font-medium group-hover:underline">{c.name}</Link>
                      {c.description && <p className="max-w-64 truncate text-xs text-muted-foreground">{c.description}</p>}
                    </TableCell>
                    <TableCell>
                      <div className="flex items-center gap-2">
                        <ClusterStateBadge state={c.lifecycle_state} />
                        {c.active_operation_id && (
                          <Link href={`/operations/${c.active_operation_id}`} className="text-xs text-sky-400 hover:underline">progress</Link>
                        )}
                      </div>
                    </TableCell>
                    <TableCell className="text-sm">{c.distribution}</TableCell>
                    <TableCell className="font-mono text-xs">{c.kubernetes_version ?? "—"}</TableCell>
                    <TableCell className="tabular-nums">{c.node_count}</TableCell>
                    <TableCell className="font-mono text-xs text-muted-foreground">{c.api_endpoint ?? "—"}</TableCell>
                    <TableCell className="text-sm text-muted-foreground">{formatRelative(c.created_at)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
