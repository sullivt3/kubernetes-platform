"use client";

import { useQuery } from "@tanstack/react-query";

import { EmptyState, PageHeader, TableSkeleton } from "@/components/common/page";
import { StatusPill } from "@/components/common/status";
import { Button } from "@/components/ui/button";
import { ClusterScope, LiveError } from "@/features/kubernetes/shared";
import { api } from "@/lib/api/client";
import { formatDateTime } from "@/lib/format";

type Report = {
  rules: { name: string; state: string; health: string; duration: number; lastError?: string; annotations?: { summary?: string; description?: string } }[];
  observed_at: string;
  coverage: Record<string, string>;
  findings: { source: string; name: string; namespace: string | null; title: string; evidence: string; guidance: string; severity: string }[];
};

function ClusterAlerts({ id }: { id: string }) {
  const query = useQuery({
    queryKey: ["clusters", id, "alerts"],
    queryFn: () => api.get<Report>(`/clusters/${id}/monitoring/alerts`),
    refetchInterval: 60000, retry: false,
  });
  return <div className="space-y-4">
    <Button variant="outline" disabled={query.isFetching} onClick={() => query.refetch()}>Refresh checks</Button>
    {query.isPending && <TableSkeleton />}
    {query.isError && <LiveError error={query.error} clusterId={id} onRetry={() => query.refetch()} />}
    {query.data && <>
      <p className="text-sm text-muted-foreground">Checked {formatDateTime(query.data.observed_at)} · refreshes every minute</p>
      <details className="rounded-lg border p-3"><summary>Check coverage</summary>
        <ul className="mt-2 space-y-1">{Object.entries(query.data.coverage).map(([source, status]) =>
          <li key={source} className="flex justify-between gap-2"><span>{source}</span><StatusPill tone={status === "ok" ? "success" : "warning"}>{status}</StatusPill></li>)}</ul>
      </details>
      {query.data.rules.length > 0 && <section className="space-y-2">
        <h2 className="font-semibold">Prometheus alert rules</h2>
        {query.data.rules.map((rule) => <article key={rule.name} className="rounded-lg border p-3">
          <div className="flex flex-wrap items-center gap-2">
            <StatusPill tone={rule.state === "firing" ? "danger" : rule.state === "pending" ? "warning" : "neutral"}>{rule.state}</StatusPill>
            <span>{rule.annotations?.summary || rule.name}</span>
            <span className="text-xs text-muted-foreground">Requires {rule.duration}s · evaluation {rule.health}</span>
          </div>
          <p className="mt-2 text-sm text-muted-foreground">{rule.annotations?.description}</p>
          {rule.lastError && <p role="alert" className="text-sm">{rule.lastError}</p>}
        </article>)}
      </section>}
      {query.data.findings.length ? query.data.findings.map((finding, index) =>
        <article key={index} className="space-y-2 rounded-lg border p-4">
          <div className="flex items-center gap-2"><StatusPill tone={finding.severity === "critical" ? "danger" : "warning"}>{finding.severity}</StatusPill>
            <h2 className="font-semibold">{finding.title}</h2></div>
          <p className="font-mono text-sm">{[finding.namespace, finding.name].filter(Boolean).join("/")}</p>
          <p className="text-sm text-muted-foreground">{finding.evidence}</p>
          <p className="text-sm">{finding.guidance}</p>
        </article>) : <EmptyState title="No current findings in the available data" description="Review check coverage for unavailable sources." />}
    </>}
  </div>;
}

export function AlertsPage() {
  return <div className="space-y-5">
    <PageHeader eyebrow="Operations" title="Alerts" description="Current operational findings and checks to help investigate them." />
    <ClusterScope>{(id) => <ClusterAlerts key={id} id={id} />}</ClusterScope>
  </div>;
}
