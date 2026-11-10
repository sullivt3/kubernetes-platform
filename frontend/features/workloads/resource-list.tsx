"use client";

import { FileCode, Plus } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { EmptyState, PageHeader, TableSkeleton } from "@/components/common/page";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { ClusterScope, LiveError } from "@/features/kubernetes/shared";
import { useClusters } from "@/lib/api/hooks";
import { useClusterNamespaces } from "@/lib/api/kube";
import { useResources, type ResourceKind } from "@/lib/api/workloads";
import { cn } from "@/lib/utils";
import { KIND_CONFIG } from "./kinds";
import { ResourceDetailDialog } from "./resource-detail";

export function ResourceTable({ clusterId, kind }: { clusterId: string; kind: ResourceKind }) {
  const config = KIND_CONFIG[kind];
  const clusterScoped = config.scope === "cluster";
  const [namespace, setNamespace] = useState("");
  const [filter, setFilter] = useState("");
  const [open, setOpen] = useState<{ namespace: string; name: string } | null>(null);
  const namespaces = useClusterNamespaces(clusterId);
  const resources = useResources(clusterId, kind, clusterScoped ? "" : namespace);
  const rows = (resources.data?.items ?? []).filter((r) => !filter || r.name.includes(filter));

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-3">
        {!clusterScoped && <Select value={namespace || "__all"} onValueChange={(v) => setNamespace(v === "__all" ? "" : v)}>
          <SelectTrigger className="w-56" aria-label="Namespace"><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="__all">All namespaces</SelectItem>
            {namespaces.data?.map((n) => <SelectItem key={n.name} value={n.name}>{n.name}</SelectItem>)}
          </SelectContent>
        </Select>}
        <Input value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="Filter by name" className="h-8 w-56" />
        {resources.data && <span className="ml-auto text-sm text-muted-foreground">{rows.length} {config.title.toLowerCase()}{resources.data.truncated ? " (truncated)" : ""}</span>}
      </div>
      {resources.isLoading && <TableSkeleton rows={5} columns={config.columns.length + 1} />}
      {resources.isError && <LiveError error={resources.error} clusterId={clusterId} onRetry={() => resources.refetch()} />}
      {resources.data && rows.length === 0 && (
        <EmptyState title={`No ${config.title.toLowerCase()}`} description={namespace ? `Nothing in namespace ${namespace}.` : "Nothing in this cluster yet."} />
      )}
      {rows.length > 0 && (
        <Card className="py-0">
          <CardContent className="px-0">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Name</TableHead>
                  {config.columns.map((c) => <TableHead key={c.header} className={c.className}>{c.header}</TableHead>)}
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map((row) => (
                  <TableRow key={`${row.namespace ?? "cluster"}/${row.name}`} className="cursor-pointer" onClick={() => setOpen({ namespace: row.namespace ?? "cluster", name: row.name })}>
                    <TableCell className="max-w-80">
                      <p className="truncate font-medium" title={row.name}>{row.name}</p>
                      <p className="text-xs text-muted-foreground">{row.namespace ?? "Cluster-scoped"}</p>
                    </TableCell>
                    {config.columns.map((c) => <TableCell key={c.header} className={cn("text-sm", c.className)}>{c.cell(row)}</TableCell>)}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      )}
      <ResourceDetailDialog clusterId={clusterId} kind={kind} target={open} onClose={() => setOpen(null)} />
    </div>
  );
}

function ScopedTitle({ clusterId, kind }: { clusterId: string; kind: ResourceKind }) {
  const config = KIND_CONFIG[kind];
  const name = useClusters().data?.find((c) => c.id === clusterId)?.name;
  return (
    <PageHeader
      eyebrow={config.eyebrow}
      title={name ? `${config.title} · ${name}` : config.title}
      description={config.description}
      actions={
        <>
          <Button asChild size="sm" variant="outline"><Link href="/kubernetes/apply"><FileCode /> Apply YAML</Link></Button>
          {kind === "deployments" && <Button asChild size="sm"><Link href="/workloads/deployments/new"><Plus /> New deployment</Link></Button>}
        </>
      }
    />
  );
}

export function ResourcePage({ kind }: { kind: ResourceKind }) {
  return (
    <div className="space-y-6">
      <ClusterScope>
        {(clusterId) => (
          <>
            <ScopedTitle clusterId={clusterId} kind={kind} />
            <ResourceTable clusterId={clusterId} kind={kind} />
          </>
        )}
      </ClusterScope>
    </div>
  );
}
