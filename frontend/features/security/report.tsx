"use client";

import { useQuery } from "@tanstack/react-query";
import { Download, RefreshCw } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { EmptyState, PageHeader, TableSkeleton } from "@/components/common/page";
import { StatusPill } from "@/components/common/status";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { ClusterScope, LiveError } from "@/features/kubernetes/shared";
import { ResourceDetailDialog } from "@/features/workloads/resource-detail";
import { apiRequest } from "@/lib/api/client";
import { formatDateTime } from "@/lib/format";

const sources = {
  namespaces: "Namespaces",
  serviceaccounts: "Service accounts",
  roles: "Roles",
  clusterroles: "ClusterRoles",
  rolebindings: "RoleBindings",
  clusterrolebindings: "ClusterRoleBindings",
  validatingadmissionpolicies: "Validating admission policies",
} as const;

const severities = {
  high: { label: "High", tone: "danger" },
  medium: { label: "Medium", tone: "warning" },
  info: { label: "Informational", tone: "neutral" },
} as const;

const coverageLabels = {
  complete: "Complete",
  truncated: "List truncated",
  denied: "Cluster credentials denied",
  unavailable: "API unavailable",
  error: "Could not read",
} as const;

type Severity = keyof typeof severities;
type Source = keyof typeof sources;
type Coverage = { source: Source; status: keyof typeof coverageLabels; scanned: number };
type Finding = {
  check: string;
  severity: Severity;
  source: Source;
  name: string;
  namespace: string | null;
  title: string;
  evidence: string;
  guidance: string;
};
type Report = {
  cluster_id: string;
  cluster_name: string;
  observed_at: string;
  coverage: Coverage[];
  findings: Finding[];
};
type InspectableFinding = Finding & { source: Exclude<Source, "namespaces"> };

const severityOptions = Object.keys(severities) as Severity[];
const pageSize = 25;

function exportReport(report: Report) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2)], { type: "application/json" }));
  const date = report.observed_at.slice(0, 10);
  const anchor = Object.assign(document.createElement("a"), {
    href: url,
    download: `security-report-${report.cluster_name}-${date}.json`,
  });
  anchor.click();
  URL.revokeObjectURL(url);
}

function Findings({ clusterId, report }: { clusterId: string; report: Report }) {
  const [severity, setSeverity] = useState<Severity | "all">("all");
  const [page, setPage] = useState(0);
  const [target, setTarget] = useState<InspectableFinding | null>(null);
  const rows = report.findings.filter((finding) => severity === "all" || finding.severity === severity);
  const pages = Math.max(1, Math.ceil(rows.length / pageSize));
  const currentPage = Math.min(page, pages - 1);
  const start = currentPage * pageSize;
  const hasGaps = report.coverage.some(({ status }) => status !== "complete");
  return (
    <section aria-labelledby="findings" className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-2">
          <h2 id="findings" className="mr-2 font-semibold">Findings</h2>
          {severityOptions.map((option) => {
            const count = report.findings.filter((finding) => finding.severity === option).length;
            return <StatusPill key={option} tone={severities[option].tone}>{count} {severities[option].label}</StatusPill>;
          })}
        </div>
        <Select value={severity} onValueChange={(value) => {
          setSeverity(value as Severity | "all");
          setPage(0);
        }}>
          <SelectTrigger aria-label="Filter findings by severity"><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All severities</SelectItem>
            {severityOptions.map((option) => <SelectItem key={option} value={option}>{severities[option].label}</SelectItem>)}
          </SelectContent>
        </Select>
      </div>
      {rows.length === 0 ? (
        <EmptyState
          title={severity === "all" ? "No findings in the available data" : "No findings for this severity"}
          description={severity === "all" && hasGaps
            ? "Coverage is incomplete. Review the missing sources before relying on this report."
            : undefined}
        />
      ) : (
        <Card className="py-0">
          <Table className="min-w-[45rem]">
            <TableHeader><TableRow>
              <TableHead>Severity</TableHead><TableHead>Finding</TableHead><TableHead>Resource</TableHead>
            </TableRow></TableHeader>
            <TableBody>
              {rows.slice(start, start + pageSize).map((finding) => {
                const { source, name, namespace } = finding;
                const level = severities[finding.severity];
                return (
                  <TableRow key={JSON.stringify([finding.check, source, namespace, name])}>
                    <TableCell className="align-top"><StatusPill tone={level.tone}>{level.label}</StatusPill></TableCell>
                    <TableCell className="max-w-lg whitespace-normal align-top">
                      <p className="font-medium">{finding.title}</p>
                      <p className="mt-1 break-words text-muted-foreground">{finding.evidence}</p>
                      <p className="mt-1">{finding.guidance}</p>
                    </TableCell>
                    <TableCell className="max-w-72 whitespace-normal align-top">
                      {source === "namespaces" ? (
                        <Link href="/security/policies" className="break-words font-mono hover:underline" title="View Pod Security policies">{name}</Link>
                      ) : (
                        <button type="button" onClick={() => setTarget({ ...finding, source })}
                          className="break-all text-left font-mono hover:underline">{name}</button>
                      )}
                      <p className="mt-1 text-xs text-muted-foreground">{sources[source]}{namespace && ` · ${namespace}`}</p>
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </Card>
      )}
      {pages > 1 && (
        <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
          <span className="text-muted-foreground">Showing {start + 1}–{Math.min(start + pageSize, rows.length)} of {rows.length}</span>
          <div className="flex gap-2">
            <Button size="sm" variant="outline" disabled={currentPage === 0} onClick={() => setPage(currentPage - 1)}>Previous</Button>
            <Button size="sm" variant="outline" disabled={currentPage >= pages - 1} onClick={() => setPage(currentPage + 1)}>Next</Button>
          </div>
        </div>
      )}
      {target && (
        <ResourceDetailDialog clusterId={clusterId} kind={target.source}
          target={{ namespace: target.namespace ?? "cluster", name: target.name }} onClose={() => setTarget(null)} />
      )}
    </section>
  );
}

function ClusterReport({ clusterId }: { clusterId: string }) {
  const query = useQuery({
    queryKey: ["clusters", clusterId, "security-report"],
    queryFn: ({ signal }) => apiRequest<Report>(`/clusters/${clusterId}/security/report`, { signal }),
    staleTime: 30_000,
    refetchOnWindowFocus: false,
    retry: false,
  });
  const report = query.data;
  const incomplete = report?.coverage.some(({ status }) => status !== "complete");
  return (
    <div className="space-y-5">
      {query.isLoading && <TableSkeleton rows={4} columns={3} />}
      {query.isError && <LiveError error={query.error} clusterId={clusterId} onRetry={() => query.refetch()} />}
      {report && (
        <>
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <h2 className="font-semibold">{report.cluster_name}</h2>
              <p className="text-sm text-muted-foreground">
                {query.isError ? "Previous report" : "Report generated"}{" "}
                <time dateTime={report.observed_at}>{formatDateTime(report.observed_at)}</time>
              </p>
            </div>
            <div className="flex flex-wrap gap-2">
              <Button size="sm" variant="outline" disabled={query.isFetching} onClick={() => query.refetch()}>
                <RefreshCw /> {query.isFetching ? "Refreshing…" : "Refresh report"}
              </Button>
              <Button size="sm" variant="outline" onClick={() => exportReport(report)}><Download /> Download JSON</Button>
            </div>
          </div>
          <details open={incomplete} className="rounded-lg border px-4 py-3">
            <summary className="cursor-pointer text-sm font-medium">
              Coverage {incomplete ? "· Incomplete" : "· All sources read"}
            </summary>
            <ul className="mt-3 divide-y text-sm">
              {report.coverage.map(({ source, status, scanned }) => (
                <li key={source} className="flex flex-wrap items-center justify-between gap-2 py-1.5">
                  <span>{sources[source]}</span>
                  <span className="flex items-center gap-2 text-muted-foreground">
                    {scanned} scanned
                    {status !== "complete" && <StatusPill tone="warning">{coverageLabels[status]}</StatusPill>}
                  </span>
                </li>
              ))}
            </ul>
          </details>
          <Findings key={report.observed_at} clusterId={clusterId} report={report} />
        </>
      )}
    </div>
  );
}

export function SecurityReportingPage() {
  return (
    <div className="space-y-5">
      <PageHeader eyebrow="Security" title="Security reporting"
        description="Review findings from the selected cluster’s declared RBAC, service accounts, and admission configuration." />
      <ClusterScope>{(clusterId) => <ClusterReport key={clusterId} clusterId={clusterId} />}</ClusterScope>
    </div>
  );
}
