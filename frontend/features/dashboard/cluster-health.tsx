"use client";

import { AlertTriangle, Boxes, CheckCircle2, CloudOff, Plus, Upload } from "lucide-react";
import Link from "next/link";

import { EmptyState, ErrorState, Section } from "@/components/common/page";
import { StatusPill } from "@/components/common/status";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { ClusterStateBadge } from "@/features/clusters/cluster-state";
import { UsageBar } from "@/features/kubernetes/shared";
import { useClusters } from "@/lib/api/hooks";
import type { ClusterSummary } from "@/lib/api/types";
import { formatRelative } from "@/lib/format";

function ClusterCard({ cluster }: { cluster: ClusterSummary }) {
  const h = cluster.health;
  return (
    <Link href={`/kubernetes/clusters/${cluster.id}`} className="group">
      <Card className="h-full transition-colors group-hover:border-foreground/20">
        <CardHeader>
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0">
              <CardTitle className="truncate">{cluster.name}</CardTitle>
              <p className="text-xs text-muted-foreground">
                {cluster.distribution} · {cluster.kubernetes_version ?? "—"}
                {cluster.observed_at && ` · observed ${formatRelative(cluster.observed_at)}`}
              </p>
            </div>
            <ClusterStateBadge state={cluster.lifecycle_state} />
          </div>
        </CardHeader>
        <CardContent className="space-y-4">
          {cluster.lifecycle_state === "unreachable" && cluster.health_error && (
            <p className="flex gap-2 text-xs text-amber-300"><CloudOff className="size-4 shrink-0" /> {cluster.health_error.message}</p>
          )}
          {!h && cluster.lifecycle_state !== "unreachable" && (
            <p className="text-sm text-muted-foreground">
              {cluster.connected ? "Waiting for the first health observation." : "Not connected yet."}
            </p>
          )}
          {h && (
            <>
              <div className="grid grid-cols-3 gap-2 text-center text-sm">
                <div>
                  <p className={h.nodes.not_ready.length ? "text-lg font-semibold text-red-400" : "text-lg font-semibold"}>{h.nodes.ready}/{h.nodes.total}</p>
                  <p className="text-xs text-muted-foreground">nodes ready</p>
                </div>
                <div>
                  <p className="text-lg font-semibold">{h.pods.running}/{h.pods.total}</p>
                  <p className="text-xs text-muted-foreground">pods running</p>
                </div>
                <div>
                  <p className={h.pods.problems ? "text-lg font-semibold text-amber-400" : "text-lg font-semibold"}>{h.pods.problems}</p>
                  <p className="text-xs text-muted-foreground">failing pods</p>
                </div>
              </div>
              <UsageBar label="CPU" kind="cpu" usage={h.cpu} />
              <UsageBar label="Memory" kind="memory" usage={h.memory} />
            </>
          )}
        </CardContent>
      </Card>
    </Link>
  );
}

export function ClusterHealthSection() {
  const clusters = useClusters();
  const list = clusters.data ?? [];
  const alerts = list.flatMap((c) => [
    ...(c.health?.alerts ?? []).map((a) => ({ ...a, cluster: c })),
    ...(c.lifecycle_state === "unreachable" ? [{ severity: "critical" as const, message: "Cluster API unreachable", cluster: c }] : []),
  ]);

  return (
    <>
      <Section
        title="Cluster health & utilization"
        description="Observed every minute from each cluster's API. Utilization shows live usage when metrics-server is installed, otherwise requested resources."
      >
        {clusters.isLoading && <Skeleton className="h-48" />}
        {clusters.isError && <ErrorState error={clusters.error} onRetry={() => clusters.refetch()} />}
        {clusters.data && list.length === 0 && (
          <EmptyState
            icon={Boxes}
            title="No clusters yet"
            description="Create a cluster on registered Linux machines, or import an existing one with its kubeconfig."
            action={
              <div className="flex gap-2">
                <Button asChild variant="outline" size="sm"><Link href="/kubernetes/import"><Upload /> Import cluster</Link></Button>
                <Button asChild size="sm"><Link href="/kubernetes/create"><Plus /> Create cluster</Link></Button>
              </div>
            }
          />
        )}
        {list.length > 0 && (
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
            {list.map((c) => <ClusterCard key={c.id} cluster={c} />)}
          </div>
        )}
      </Section>
      {list.length > 0 && (
        <Section title="Alerts" description="From the latest observation of each cluster">
          <Card>
            <CardContent>
              {alerts.length === 0 ? (
                <p className="flex items-center gap-2 text-sm text-emerald-300"><CheckCircle2 className="size-4" /> No problems detected across {list.length} cluster(s).</p>
              ) : (
                <ul className="space-y-2">
                  {alerts.map((a, i) => (
                    <li key={`${a.cluster.id}-${i}`} className="flex flex-wrap items-center gap-2 text-sm">
                      <StatusPill tone={a.severity === "critical" ? "danger" : "warning"} icon={AlertTriangle}>{a.severity}</StatusPill>
                      <Link href={`/kubernetes/clusters/${a.cluster.id}`} className="font-medium hover:underline">{a.cluster.name}</Link>
                      <span className="text-muted-foreground">{a.message}</span>
                    </li>
                  ))}
                </ul>
              )}
            </CardContent>
          </Card>
        </Section>
      )}
    </>
  );
}
