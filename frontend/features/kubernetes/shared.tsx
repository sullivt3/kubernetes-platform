"use client";

import { Boxes, CloudOff, Plus, Upload } from "lucide-react";
import Link from "next/link";
import type { ReactNode } from "react";

import { EmptyState, ErrorState } from "@/components/common/page";
import { StatusPill } from "@/components/common/status";
import { useSelectedCluster } from "@/components/layout/cluster-context";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { ClusterStateBadge } from "@/features/clusters/cluster-state";
import { ApiError } from "@/lib/api/client";
import { useClusters } from "@/lib/api/hooks";
import { isUnreachable, type ResourceUsage } from "@/lib/api/kube";
import { formatBytes, formatCpu, formatRelative } from "@/lib/format";
import { cn } from "@/lib/utils";

/** Shows why live data is unavailable (unreachable / auth) or a generic error. */
export function LiveError({ error, clusterId, onRetry }: { error: unknown; clusterId: string; onRetry?: () => void }) {
  if (isUnreachable(error)) {
    const apiError = error as ApiError;
    return (
      <div className="flex gap-3 rounded-lg border border-amber-500/30 bg-amber-500/5 p-4 text-sm" role="alert">
        <CloudOff className="mt-0.5 size-5 shrink-0 text-amber-400" />
        <div className="space-y-1">
          <p className="font-medium text-amber-200">
            {apiError.code === "cluster_auth_failed" ? "The cluster rejected the platform's credentials" : "The cluster's API server is unreachable"}
          </p>
          <p className="text-muted-foreground">{apiError.message}</p>
          <p className="text-muted-foreground">
            The platform keeps working. Check the{" "}
            <Link href={`/kubernetes/clusters/${clusterId}?tab=settings`} className="text-foreground underline underline-offset-4">
              connection settings
            </Link>
            {onRetry && (
              <>
                {" "}or{" "}
                <button type="button" className="text-foreground underline underline-offset-4" onClick={onRetry}>
                  try again
                </button>
              </>
            )}
            .
          </p>
        </div>
      </div>
    );
  }
  return <ErrorState error={error} onRetry={onRetry} />;
}

export function UsageBar({ label, usage, kind }: { label: string; usage: ResourceUsage; kind: "cpu" | "memory" }) {
  const total = (kind === "cpu" ? usage.allocatable_m : usage.allocatable_bytes) ?? 0;
  const requested = (kind === "cpu" ? usage.requested_m : usage.requested_bytes) ?? 0;
  const used = kind === "cpu" ? usage.used_m : usage.used_bytes;
  const fmt = kind === "cpu" ? formatCpu : formatBytes;
  const pct = (v: number) => (total ? Math.min(100, (v / total) * 100) : 0);
  const shown = used ?? requested;
  return (
    <div className="space-y-1.5">
      <div className="flex items-baseline justify-between text-sm">
        <span className="font-medium">{label}</span>
        <span className="tabular-nums text-muted-foreground">
          {fmt(shown)} / {fmt(total)} <span className="text-xs">({Math.round(pct(shown))}% {used !== null && used !== undefined ? "used" : "requested"})</span>
        </span>
      </div>
      <div className="relative h-2 overflow-hidden rounded-full bg-muted" aria-hidden>
        <div className="absolute inset-y-0 left-0 bg-sky-500/35" style={{ width: `${pct(requested)}%` }} />
        {used !== null && used !== undefined && (
          <div className={cn("absolute inset-y-0 left-0", pct(used) > 85 ? "bg-red-400" : pct(used) > 70 ? "bg-amber-400" : "bg-emerald-400")} style={{ width: `${pct(used)}%` }} />
        )}
      </div>
      <p className="text-[11px] text-muted-foreground">
        {used !== null && used !== undefined ? `Requested ${fmt(requested)} · measured by metrics-server` : "Live usage needs metrics-server; showing requested resources"}
      </p>
    </div>
  );
}

export function NodeReadyBadge({ ready, status }: { ready: boolean; status: string }) {
  return <StatusPill tone={ready ? (status.includes("SchedulingDisabled") ? "warning" : "success") : "danger"}>{status}</StatusPill>;
}

export function PodStatusBadge({ status, problem, phase }: { status: string; problem: boolean; phase: string }) {
  const tone = problem || phase === "Failed" ? "danger" : status === "Running" || status === "Completed" || phase === "Succeeded" ? "success" : phase === "Pending" ? "warning" : "neutral";
  return <StatusPill tone={tone}>{status}</StatusPill>;
}

/**
 * Renders children for the cluster chosen in the top-bar selector, or a picker when
 * none is selected. Pages like Nodes and Pods operate on this cluster.
 */
export function ClusterScope({ children }: { children: (clusterId: string) => ReactNode }) {
  const clusters = useClusters();
  const { clusterId, setClusterId } = useSelectedCluster();
  if (clusters.isLoading) return <Skeleton className="h-64" />;
  if (clusters.isError) return <ErrorState error={clusters.error} onRetry={() => clusters.refetch()} />;
  const connected = (clusters.data ?? []).filter((c) => c.connected);
  const selected = connected.find((c) => c.id === clusterId);
  if (selected) return <>{children(selected.id)}</>;
  if (connected.length === 0) {
    return (
      <EmptyState
        icon={Boxes}
        title="No connected clusters"
        description="Import an existing cluster with a kubeconfig, or create one on registered machines."
        action={
          <div className="flex gap-2">
            <Button asChild variant="outline" size="sm"><Link href="/kubernetes/import"><Upload /> Import cluster</Link></Button>
            <Button asChild size="sm"><Link href="/kubernetes/create"><Plus /> Create cluster</Link></Button>
          </div>
        }
      />
    );
  }
  return (
    <div className="space-y-3">
      <p className="text-sm text-muted-foreground">Choose a cluster (you can switch any time from the top bar):</p>
      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        {connected.map((c) => (
          <button
            key={c.id}
            type="button"
            onClick={() => setClusterId(c.id)}
            className="flex flex-col gap-1 rounded-lg border border-border p-4 text-left hover:border-foreground/30"
          >
            <span className="flex items-center justify-between gap-2">
              <span className="font-medium">{c.name}</span>
              <ClusterStateBadge state={c.lifecycle_state} />
            </span>
            <span className="text-xs text-muted-foreground">
              {c.kubernetes_version ?? "unknown version"} · {c.health ? `${c.health.nodes.ready}/${c.health.nodes.total} nodes ready` : "not observed yet"}
              {c.observed_at && ` · ${formatRelative(c.observed_at)}`}
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}
