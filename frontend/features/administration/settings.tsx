"use client";

import { ErrorState, PageHeader, Section } from "@/components/common/page";
import { HealthBadge } from "@/components/common/status";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { useReadiness, useSystemInfo } from "@/lib/api/hooks";

function Rows({ rows }: { rows: [string, React.ReactNode][] }) {
  return (
    <dl className="divide-y divide-border/50 text-sm">
      {rows.map(([label, value]) => (
        <div key={label} className="flex justify-between gap-6 py-2.5">
          <dt className="text-muted-foreground">{label}</dt>
          <dd className="text-right font-medium">{value}</dd>
        </div>
      ))}
    </dl>
  );
}

export function SettingsPage() {
  const info = useSystemInfo();
  const readiness = useReadiness();
  return (
    <div className="space-y-8">
      <PageHeader
        eyebrow="Administration"
        title="Settings"
        description="Effective platform configuration. Values are set through environment variables on the backend; secrets are never displayed."
      />
      <Section title="Platform">
        <Card>
          <CardContent>
            {info.isLoading && <Skeleton className="h-48" />}
            {info.isError && <ErrorState error={info.error} onRetry={() => info.refetch()} />}
            {info.data && (
              <Rows
                rows={[
                  ["Application", info.data.app_name],
                  ["Version", info.data.version],
                  ["Environment", info.data.environment],
                  ["Authentication mode", info.data.auth_mode === "dev" ? "dev — local administrator (not for production)" : info.data.auth_mode],
                  ["Signed in as", <span key="u" className="font-mono text-xs">{info.data.current_user.subject}</span>],
                  ["Roles", info.data.current_user.roles.join(", ")],
                  ["Operation execution", info.data.orchestration_mode],
                  ["Secret storage", `${info.data.secret_backend} (primary key "${info.data.secret_primary_key_id}")`],
                ]}
              />
            )}
          </CardContent>
        </Card>
      </Section>
      <Section title="Dependencies" description="Live readiness of the services the platform depends on">
        <Card>
          <CardContent>
            {readiness.isLoading && <Skeleton className="h-32" />}
            {readiness.isError && <ErrorState error={readiness.error} onRetry={() => readiness.refetch()} />}
            {readiness.data && (
              <Rows
                rows={readiness.data.components.map((c) => [
                  c.name,
                  <span key={c.name} className="flex items-center justify-end gap-3">
                    <span className="text-xs font-normal text-muted-foreground">{c.message}</span>
                    <HealthBadge status={c.status} />
                  </span>,
                ])}
              />
            )}
          </CardContent>
        </Card>
      </Section>
      <Section title="API">
        <Card>
          <CardContent className="text-sm text-muted-foreground">
            Interactive API documentation is available at{" "}
            <a className="font-mono text-foreground underline underline-offset-4" href="/api/v1/docs" target="_blank" rel="noreferrer">
              /api/v1/docs
            </a>
            .
          </CardContent>
        </Card>
      </Section>
    </div>
  );
}
