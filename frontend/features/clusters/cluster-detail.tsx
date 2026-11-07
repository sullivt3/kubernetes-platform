"use client";

import { ArrowLeft, KeyRound, Trash2 } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
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
import { OperationsTable } from "@/features/operations/operations-table";
import { ApiError } from "@/lib/api/client";
import { useCluster, useDeleteCluster } from "@/lib/api/hooks";
import { formatDateTime } from "@/lib/format";
import { ClusterStateBadge } from "./cluster-state";

function BackLink() {
  return (
    <Button asChild variant="ghost" size="sm" className="-ml-2">
      <Link href="/kubernetes/clusters"><ArrowLeft /> All clusters</Link>
    </Button>
  );
}

export function ClusterDetailView({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const router = useRouter();
  const cluster = useCluster(id);
  const remove = useDeleteCluster();
  const [deleting, setDeleting] = useState(false);
  const [force, setForce] = useState(false);

  if (cluster.isLoading) return <div className="space-y-4"><Skeleton className="h-16" /><Skeleton className="h-80" /></div>;
  if (cluster.isError || !cluster.data) {
    const missing = cluster.error instanceof ApiError && cluster.error.status === 404;
    return (
      <div className="space-y-4">
        <BackLink />
        <ErrorState
          title={missing ? "Cluster not found" : "Could not load cluster"}
          error={missing ? new Error("It may have been deleted.") : cluster.error}
          onRetry={missing ? undefined : () => cluster.refetch()}
        />
      </div>
    );
  }

  const c = cluster.data;
  const busy = Boolean(c.active_operation_id);
  const network = c.spec.network;
  const facts: [string, React.ReactNode][] = [
    ["Distribution", c.distribution],
    ["Kubernetes", c.kubernetes_version ?? `${c.spec.kubernetes_version} (requested)`],
    ["API endpoint", <span key="e" className="font-mono text-xs">{c.api_endpoint ?? "—"}</span>],
    ["Pod network", `${network.cni} · ${network.pod_cidr}`],
    ["Service network", network.service_cidr],
    ["DNS domain", network.dns_domain],
    ["Container runtime", c.spec.container_runtime ?? "containerd"],
    ["Created", formatDateTime(c.created_at)],
  ];

  return (
    <div className="space-y-6">
      <BackLink />
      <PageHeader
        eyebrow="Cluster"
        title={c.name}
        description={c.description ?? undefined}
        actions={
          <>
            <ClusterStateBadge state={c.lifecycle_state} />
            {c.active_operation_id && (
              <Button asChild size="sm" variant="outline">
                <Link href={`/operations/${c.active_operation_id}`}>View progress</Link>
              </Button>
            )}
            <Button size="sm" variant="outline" disabled={busy} onClick={() => setDeleting(true)}>
              <Trash2 className="text-red-400" /> Delete
            </Button>
          </>
        }
      />

      {c.lifecycle_state === "error" && c.status_detail.error && (
        <ErrorState
          title={`Last operation failed${c.status_detail.error.step ? ` at "${c.status_detail.error.step}"` : ""}`}
          error={new Error(c.status_detail.error.message)}
          onRetry={
            c.status_detail.operation_id
              ? () => router.push(`/operations/${c.status_detail.operation_id}`)
              : undefined
          }
        />
      )}

      <div className="grid gap-6 lg:grid-cols-3">
        <Card className="lg:col-span-2 py-0">
          <CardContent className="px-0">
            <div className="px-4 pt-4 pb-2 text-sm font-semibold">Nodes</div>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Node</TableHead>
                  <TableHead>Role</TableHead>
                  <TableHead>Address</TableHead>
                  <TableHead>OS</TableHead>
                  <TableHead>Resources</TableHead>
                  <TableHead>Membership</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {c.nodes.map((n) => (
                  <TableRow key={n.machine_id}>
                    <TableCell className="font-medium">{n.node_name ?? n.machine_name}</TableCell>
                    <TableCell className="text-sm">{n.role === "control_plane" ? "Control plane" : "Worker"}</TableCell>
                    <TableCell className="font-mono text-xs">{n.address ?? "—"}</TableCell>
                    <TableCell className="text-sm text-muted-foreground">{n.os ?? "—"}</TableCell>
                    <TableCell className="text-sm tabular-nums text-muted-foreground">
                      {n.cpu_cores ? `${n.cpu_cores} CPU · ${Math.round(((n.memory_mb ?? 0) / 1024) * 10) / 10} GB` : "—"}
                    </TableCell>
                    <TableCell>
                      <StatusPill
                        tone={n.membership_state === "joined" ? "success" : n.membership_state === "failed" ? "danger" : "info"}
                      >
                        {n.membership_state}
                      </StatusPill>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            <p className="px-4 py-3 text-xs text-muted-foreground">
              Node assignments as recorded by the platform. Live node status from the Kubernetes API arrives in Phase 1.
            </p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="space-y-4">
            <h2 className="text-sm font-semibold">Configuration</h2>
            <dl className="space-y-2 text-sm">
              {facts.map(([label, value]) => (
                <div key={label} className="flex justify-between gap-4">
                  <dt className="text-muted-foreground">{label}</dt>
                  <dd className="truncate text-right">{value}</dd>
                </div>
              ))}
            </dl>
            <div className="flex gap-2 rounded-md border border-border/70 p-3 text-xs text-muted-foreground">
              <KeyRound className="mt-0.5 size-4 shrink-0" />
              {c.has_kubeconfig
                ? "The administrator kubeconfig is stored encrypted as a platform credential. It is never sent to the browser."
                : "No kubeconfig stored yet — it is saved when installation completes."}
            </div>
          </CardContent>
        </Card>
      </div>

      <Section title="Operations" description="Installation and lifecycle operations for this cluster">
        <Card className="py-0">
          <CardContent className="px-0">
            {c.operations.length ? (
              <OperationsTable operations={c.operations} compact />
            ) : (
              <p className="p-4 text-sm text-muted-foreground">No operations recorded.</p>
            )}
          </CardContent>
        </Card>
      </Section>

      <ConfirmDialog
        open={deleting}
        onOpenChange={(open) => {
          setDeleting(open);
          if (!open) setForce(false);
        }}
        title={`Delete cluster ${c.name}?`}
        description={
          <>
            <p>
              Runs <span className="font-mono">kubeadm reset</span> on all {c.nodes.length} node(s), removing Kubernetes
              state and every workload. The stored kubeconfig is deleted and the machines return to the inventory.
              Installed packages remain.
            </p>
            <div className="flex items-start gap-2 pt-1">
              <input
                id="force-delete"
                type="checkbox"
                className="mt-0.5"
                checked={force}
                onChange={(e) => setForce(e.target.checked)}
              />
              <Label htmlFor="force-delete" className="text-sm font-normal leading-snug">
                Continue even if some nodes are unreachable (their Kubernetes state will be left behind)
              </Label>
            </div>
          </>
        }
        confirmText={c.name}
        confirmLabel="Delete cluster"
        pending={remove.isPending}
        onConfirm={() =>
          remove.mutate(
            { id: c.id, confirm: c.name, force },
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
