"use client";

import { ArrowLeft } from "lucide-react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { use, useState } from "react";
import { toast } from "sonner";

import { ConfirmDialog } from "@/components/common/confirm-dialog";
import { ErrorState, PageHeader, Section } from "@/components/common/page";
import { StatusPill } from "@/components/common/status";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ClusterOverview, ClusterSettings } from "@/features/kubernetes/cluster-panels";
import { EventsTable, NamespacesTable, NodesTable, PodsTable, TopologyView } from "@/features/kubernetes/resources";
import { OperationsTable } from "@/features/operations/operations-table";
import { ApiError } from "@/lib/api/client";
import { useCluster, useDeleteCluster } from "@/lib/api/hooks";
import type { ClusterDetail } from "@/lib/api/types";
import { formatRelative } from "@/lib/format";
import { ClusterStateBadge } from "./cluster-state";
import { WorkerPools } from "./worker-pools";

const TABS = ["overview", "nodes", "namespaces", "pods", "events", "topology", "settings", "operations"] as const;
type Tab = (typeof TABS)[number];
const LIVE_TABS: Tab[] = ["overview", "nodes", "namespaces", "pods", "events", "topology"];

function BackLink() {
  return (
    <Button asChild variant="ghost" size="sm" className="-ml-2">
      <Link href="/kubernetes/clusters"><ArrowLeft /> All clusters</Link>
    </Button>
  );
}

function InstallationNodes({ cluster }: { cluster: ClusterDetail }) {
  if (cluster.origin !== "provisioned" || cluster.nodes.length === 0) return null;
  return (
    <Section title="Machines" description="Inventory machines this cluster was installed on">
      <Card className="py-0">
        <CardContent className="px-0">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Machine</TableHead>
                <TableHead>Role</TableHead>
                <TableHead>Address</TableHead>
                <TableHead>OS</TableHead>
                <TableHead>Membership</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {cluster.nodes.map((n) => (
                <TableRow key={n.machine_id}>
                  <TableCell className="font-medium">{n.machine_name}</TableCell>
                  <TableCell className="text-sm">{n.role === "control_plane" ? "Control plane" : "Worker"}</TableCell>
                  <TableCell className="font-mono text-xs">{n.address ?? "—"}</TableCell>
                  <TableCell className="text-sm text-muted-foreground">{n.os ?? "—"}</TableCell>
                  <TableCell>
                    <StatusPill tone={n.membership_state === "joined" ? "success" : n.membership_state === "failed" ? "danger" : "info"}>{n.membership_state}</StatusPill>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </Section>
  );
}

function NotConnected({ cluster }: { cluster: ClusterDetail }) {
  return (
    <div className="rounded-lg border border-border p-4 text-sm text-muted-foreground">
      {cluster.origin === "provisioned"
        ? "Live data becomes available once installation finishes and the kubeconfig is stored."
        : "The platform has no credentials for this cluster."}
      {cluster.active_operation_id && (
        <>
          {" "}
          <Link href={`/operations/${cluster.active_operation_id}`} className="text-foreground underline underline-offset-4">View progress</Link>
        </>
      )}
    </div>
  );
}

export function ClusterDetailView({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const router = useRouter();
  const search = useSearchParams();
  const requested = search.get("tab") as Tab | null;
  const [tab, setTab] = useState<Tab>(requested && TABS.includes(requested) ? requested : "overview");
  const cluster = useCluster(id);
  const remove = useDeleteCluster();
  const [deleting, setDeleting] = useState(false);
  const [force, setForce] = useState(false);
  const [destroyMachines, setDestroyMachines] = useState(false);

  if (cluster.isLoading) return <div className="space-y-4"><Skeleton className="h-16" /><Skeleton className="h-80" /></div>;
  if (cluster.isError || !cluster.data) {
    const missing = cluster.error instanceof ApiError && cluster.error.status === 404;
    return (
      <div className="space-y-4">
        <BackLink />
        <ErrorState
          title={missing ? "Cluster not found" : "Could not load cluster"}
          error={missing ? new Error("It may have been deleted or unregistered.") : cluster.error}
          onRetry={missing ? undefined : () => cluster.refetch()}
        />
      </div>
    );
  }

  const c = cluster.data;
  const live = (content: React.ReactNode) => (c.connected ? content : <NotConnected cluster={c} />);

  return (
    <div className="space-y-6">
      <BackLink />
      <PageHeader
        eyebrow={c.origin === "imported" ? "Imported cluster" : "Cluster"}
        title={c.name}
        description={
          <>
            {c.distribution} · {c.kubernetes_version ?? "version unknown"}
            {c.api_endpoint && <> · <span className="font-mono text-xs">{c.api_server_override ?? c.api_endpoint}</span></>}
            {c.observed_at && <> · observed {formatRelative(c.observed_at)}</>}
          </>
        }
        actions={
          <>
            <ClusterStateBadge state={c.lifecycle_state} />
            {c.active_operation_id && (
              <Button asChild size="sm" variant="outline"><Link href={`/operations/${c.active_operation_id}`}>View progress</Link></Button>
            )}
          </>
        }
      />

      {c.lifecycle_state === "error" && c.status_detail.error && (
        <ErrorState
          title={`Last operation failed${c.status_detail.error.step ? ` at "${c.status_detail.error.step}"` : ""}`}
          error={new Error(c.status_detail.error.message)}
          onRetry={c.status_detail.operation_id ? () => router.push(`/operations/${c.status_detail.operation_id}`) : undefined}
        />
      )}

      <Tabs value={tab} onValueChange={(v) => setTab(v as Tab)}>
        <TabsList className="flex-wrap">
          {TABS.map((t) => (
            <TabsTrigger key={t} value={t} disabled={LIVE_TABS.includes(t) && t !== "overview" && !c.connected} className="capitalize">
              {t}
            </TabsTrigger>
          ))}
        </TabsList>
        <TabsContent value="overview" className="space-y-6 pt-4">
          {live(<ClusterOverview cluster={c} />)}
          <InstallationNodes cluster={c} />
          <WorkerPools cluster={c} />
        </TabsContent>
        <TabsContent value="nodes" className="pt-4">{live(<NodesTable clusterId={c.id} />)}</TabsContent>
        <TabsContent value="namespaces" className="pt-4">{live(<NamespacesTable clusterId={c.id} />)}</TabsContent>
        <TabsContent value="pods" className="pt-4">{live(<PodsTable clusterId={c.id} />)}</TabsContent>
        <TabsContent value="events" className="pt-4">{live(<EventsTable clusterId={c.id} />)}</TabsContent>
        <TabsContent value="topology" className="pt-4">{live(<TopologyView clusterId={c.id} />)}</TabsContent>
        <TabsContent value="settings" className="pt-4"><ClusterSettings cluster={c} onDelete={() => setDeleting(true)} /></TabsContent>
        <TabsContent value="operations" className="pt-4">
          <Card className="py-0">
            <CardContent className="px-0">
              {c.operations.length ? <OperationsTable operations={c.operations} compact /> : <p className="p-4 text-sm text-muted-foreground">No operations recorded.</p>}
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>

      <ConfirmDialog
        open={deleting}
        onOpenChange={(open) => {
          setDeleting(open);
          if (!open) {
            setForce(false);
            setDestroyMachines(false);
          }
        }}
        title={`Delete cluster ${c.name}?`}
        description={
          <>
            <p>
              Runs <span className="font-mono">kubeadm reset</span> on all {c.nodes.length} node(s), removing Kubernetes state
              and every workload. The stored kubeconfig is deleted and the machines return to the inventory. Installed
              packages remain.
            </p>
            <div className="flex items-start gap-2 pt-1">
              <input id="force-delete" type="checkbox" className="mt-0.5" checked={force} onChange={(e) => setForce(e.target.checked)} />
              <Label htmlFor="force-delete" className="text-sm font-normal leading-snug">
                Continue even if some nodes are unreachable (their Kubernetes state will be left behind)
              </Label>
            </div>
            {c.nodes.some((n) => n.provisioned) && (
              <div className="flex items-start gap-2">
                <input id="destroy-machines" type="checkbox" className="mt-0.5" checked={destroyMachines} onChange={(e) => setDestroyMachines(e.target.checked)} />
                <Label htmlFor="destroy-machines" className="text-sm font-normal leading-snug">
                  Also destroy the {c.nodes.filter((n) => n.provisioned).length} machine(s) the platform provisioned for it
                  (registered servers are only released)
                </Label>
              </div>
            )}
          </>
        }
        confirmText={c.name}
        confirmLabel="Delete cluster"
        pending={remove.isPending}
        onConfirm={() =>
          remove.mutate(
            { id: c.id, confirm: c.name, force, destroyMachines },
            {
              onSuccess: (op) => {
                setDeleting(false);
                router.push(`/operations/${op.id}`);
              },
              onError: (e) => toast.error("Could not delete cluster", { description: e.message }),
            },
          )
        }
      />
    </div>
  );
}
