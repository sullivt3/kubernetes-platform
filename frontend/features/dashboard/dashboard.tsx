"use client";

import { Boxes, Cpu, Server, Workflow } from "lucide-react";
import Link from "next/link";

import { EmptyState, ErrorState, PageHeader, Section, TableSkeleton } from "@/components/common/page";
import { HealthBadge, HEALTH_TONE, StatusDot } from "@/components/common/status";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { ClusterHealthSection } from "@/features/dashboard/cluster-health";
import { OperationsTable } from "@/features/operations/operations-table";
import { SelfCheckButton } from "@/features/operations/self-check-button";
import {
  useClusters,
  useDistributions,
  useOperations,
  useProviders,
  useReadiness,
  useSystemInfo,
} from "@/lib/api/hooks";
import { formatRelative, humanize } from "@/lib/format";

const COMPONENT_LABELS: Record<string, string> = {
  database: "PostgreSQL",
  redis: "Redis",
  workers: "Orchestration workers",
  secret_store: "Credential encryption",
};

function PlatformHealthCard() {
  const readiness = useReadiness();
  return (
    <Card className="lg:col-span-2">
      <CardHeader>
        <div className="flex items-center justify-between gap-2">
          <div>
            <CardTitle>Platform health</CardTitle>
            <CardDescription>
              Management plane dependencies
              {readiness.data && ` · checked ${formatRelative(readiness.data.checked_at)}`}
            </CardDescription>
          </div>
          {readiness.data && <HealthBadge status={readiness.data.status} />}
        </div>
      </CardHeader>
      <CardContent>
        {readiness.isLoading && <TableSkeleton rows={4} columns={2} />}
        {readiness.isError && (
          <ErrorState
            title="Platform API unreachable"
            error={readiness.error}
            onRetry={() => readiness.refetch()}
          />
        )}
        {readiness.data && (
          <ul className="divide-y divide-border/60">
            {readiness.data.components.map((component) => (
              <li key={component.name} className="flex items-start justify-between gap-4 py-2.5">
                <div className="flex gap-3">
                  <span className="mt-1.5">
                    <StatusDot tone={HEALTH_TONE[component.status]} />
                  </span>
                  <div>
                    <p className="text-sm font-medium">{COMPONENT_LABELS[component.name] ?? humanize(component.name)}</p>
                    <p className="text-xs text-muted-foreground">{component.message}</p>
                  </div>
                </div>
                {component.latency_ms !== null && (
                  <span className="shrink-0 font-mono text-xs text-muted-foreground">{component.latency_ms} ms</span>
                )}
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

function StatCard({
  title,
  icon: Icon,
  value,
  detail,
  href,
  loading,
}: {
  title: string;
  icon: React.ComponentType<{ className?: string }>;
  value: React.ReactNode;
  detail: React.ReactNode;
  href: string;
  loading?: boolean;
}) {
  return (
    <Link href={href} className="group">
      <Card className="h-full transition-colors group-hover:border-foreground/20">
        <CardHeader>
          <CardDescription className="flex items-center gap-2">
            <Icon className="size-4" /> {title}
          </CardDescription>
          {loading ? <Skeleton className="h-8 w-16" /> : <CardTitle className="text-2xl tabular-nums">{value}</CardTitle>}
          <p className="text-xs text-muted-foreground">{detail}</p>
        </CardHeader>
      </Card>
    </Link>
  );
}

function SummaryCards() {
  const clusters = useClusters();
  const providers = useProviders();
  const distributions = useDistributions();
  const active = useOperations({ active: true, limit: 1 });
  const implemented = (items?: { implementation_status: string }[]) =>
    items?.filter((i) => i.implementation_status !== "planned").length ?? 0;

  return (
    <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
      <StatCard
        title="Clusters"
        icon={Boxes}
        href="/kubernetes/clusters"
        loading={clusters.isLoading}
        value={clusters.data?.length ?? "—"}
        detail={clusters.isError ? "Could not load clusters" : "Registered with the platform"}
      />
      <StatCard
        title="Active operations"
        icon={Workflow}
        href="/operations"
        loading={active.isLoading}
        value={active.data?.total ?? "—"}
        detail={active.isError ? "Could not load operations" : "Queued or running now"}
      />
      <StatCard
        title="Infrastructure providers"
        icon={Server}
        href="/infrastructure/providers"
        loading={providers.isLoading}
        value={providers.data ? `${implemented(providers.data)} / ${providers.data.length}` : "—"}
        detail="Implemented / catalogued"
      />
      <StatCard
        title="Kubernetes distributions"
        icon={Cpu}
        href="/kubernetes/distributions"
        loading={distributions.isLoading}
        value={distributions.data ? `${implemented(distributions.data)} / ${distributions.data.length}` : "—"}
        detail="Implemented / catalogued"
      />
    </div>
  );
}

function SystemCard() {
  const info = useSystemInfo();
  const rows: [string, string | undefined][] = [
    ["Version", info.data?.version],
    ["Environment", info.data?.environment],
    ["Authentication", info.data?.auth_mode === "dev" ? "Development (local admin)" : info.data?.auth_mode],
    ["Execution backend", info.data?.orchestration_mode],
    ["Credential encryption", info.data && `${info.data.secret_backend} · key ${info.data.secret_primary_key_id}`],
  ];
  return (
    <Card>
      <CardHeader>
        <CardTitle>Management plane</CardTitle>
        <CardDescription>Runs independently of every managed cluster</CardDescription>
      </CardHeader>
      <CardContent>
        {info.isError ? (
          <ErrorState error={info.error} onRetry={() => info.refetch()} />
        ) : (
          <dl className="space-y-2 text-sm">
            {rows.map(([label, value]) => (
              <div key={label} className="flex justify-between gap-4">
                <dt className="text-muted-foreground">{label}</dt>
                <dd className="truncate text-right font-medium">{value ?? <Skeleton className="h-4 w-20" />}</dd>
              </div>
            ))}
          </dl>
        )}
      </CardContent>
    </Card>
  );
}

function RecentOperations() {
  const operations = useOperations({ limit: 8 });
  return (
    <Section
      title="Recent operations"
      description="Long-running work executed by the orchestration engine"
      actions={
        <Button asChild variant="ghost" size="sm">
          <Link href="/operations">View all</Link>
        </Button>
      }
    >
      <Card className="py-0">
        <CardContent className="px-0">
          {operations.isLoading && (
            <div className="p-4">
              <TableSkeleton rows={3} columns={5} />
            </div>
          )}
          {operations.isError && (
            <div className="p-4">
              <ErrorState error={operations.error} onRetry={() => operations.refetch()} />
            </div>
          )}
          {operations.data?.items.length === 0 && (
            <EmptyState
              className="m-4"
              icon={Workflow}
              title="No operations yet"
              description="Run the platform self-check to verify the orchestration engine end to end."
              action={<SelfCheckButton variant="outline" />}
            />
          )}
          {operations.data && operations.data.items.length > 0 && (
            <OperationsTable operations={operations.data.items} compact />
          )}
        </CardContent>
      </Card>
    </Section>
  );
}

export function Dashboard() {
  return (
    <div className="space-y-8">
      <PageHeader
        title="Overview"
        description="Health of the management platform, infrastructure and clusters."
        actions={<SelfCheckButton />}
      />
      <SummaryCards />
      <div className="grid gap-4 lg:grid-cols-3">
        <PlatformHealthCard />
        <SystemCard />
      </div>
      <ClusterHealthSection />
      <RecentOperations />
    </div>
  );
}
