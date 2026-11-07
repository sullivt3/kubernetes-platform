"use client";

import { Info } from "lucide-react";

import { ErrorState, PageHeader } from "@/components/common/page";
import { ImplementationBadge } from "@/components/common/status";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { ConnectionsSection } from "@/features/infrastructure/connections";
import { useDistributions, useProviders } from "@/lib/api/hooks";
import type { JsonSchema } from "@/lib/api/types";
import { humanize } from "@/lib/format";

function CatalogNotice({ what }: { what: string }) {
  return (
    <div className="flex gap-3 rounded-lg border border-sky-500/20 bg-sky-500/5 p-4 text-sm">
      <Info className="mt-0.5 size-4 shrink-0 text-sky-400" />
      <p className="text-muted-foreground">
        This catalog is served by the backend and shows the {what} the platform architecture supports, with the
        capabilities and configuration each will expose. Entries marked <em>Planned</em> are not implemented yet and
        cannot be used: the backend refuses to run them rather than simulate success.
      </p>
    </div>
  );
}

function ConfigFields({ schema }: { schema: JsonSchema }) {
  const properties = Object.entries(schema.properties ?? {});
  if (!properties.length) return <p className="text-xs text-muted-foreground">No configuration required.</p>;
  const required = new Set(schema.required ?? []);
  return (
    <ul className="space-y-1">
      {properties.map(([name, prop]) => (
        <li key={name} className="flex items-baseline justify-between gap-3 text-xs">
          <span className="font-mono">
            {name}
            {required.has(name) && <span className="text-red-400">*</span>}
          </span>
          <span className="truncate text-right text-muted-foreground">{prop.description ?? prop.title}</span>
        </li>
      ))}
    </ul>
  );
}

function CardGridSkeleton() {
  return (
    <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
      {Array.from({ length: 6 }).map((_, i) => (
        <Skeleton key={i} className="h-64" />
      ))}
    </div>
  );
}

export function ProviderCatalog() {
  const providers = useProviders();
  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow="Infrastructure"
        title="Infrastructure providers"
        description="Where machines come from: physical servers over SSH, local Docker nodes, virtualization platforms and public clouds. Providers manage machine lifecycle only; Kubernetes installation is handled separately by distributions."
      />
      <ConnectionsSection providers={providers.data} />
      <h2 className="pt-2 font-semibold">Supported providers</h2>
      <CatalogNotice what="infrastructure providers" />
      {providers.isLoading && <CardGridSkeleton />}
      {providers.isError && <ErrorState error={providers.error} onRetry={() => providers.refetch()} />}
      {providers.data && (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {providers.data.map((p) => (
            <Card key={p.type}>
              <CardHeader>
                <div className="flex items-start justify-between gap-2">
                  <div>
                    <CardTitle>{p.display_name}</CardTitle>
                    <CardDescription className="capitalize">{p.category}</CardDescription>
                  </div>
                  <ImplementationBadge status={p.implementation_status} phase={p.planned_phase} />
                </div>
                <p className="text-sm text-muted-foreground">{p.description}</p>
              </CardHeader>
              <CardContent className="space-y-4">
                <div>
                  <p className="mb-1.5 text-xs font-medium text-muted-foreground">Capabilities</p>
                  <div className="flex flex-wrap gap-1">
                    {p.capabilities.map((c) => (
                      <Badge key={c} variant="secondary" className="font-normal">
                        {humanize(c)}
                      </Badge>
                    ))}
                    {p.planned_capabilities.map((c) => (
                      <Badge key={c} variant="outline" className="font-normal text-muted-foreground" title={`Planned for Phase ${p.planned_capabilities_phase}`}>
                        {humanize(c)} · P{p.planned_capabilities_phase}
                      </Badge>
                    ))}
                  </div>
                </div>
                <div>
                  <p className="mb-1.5 text-xs font-medium text-muted-foreground">Credentials</p>
                  <p className="text-xs">
                    {p.credential_kinds.length ? p.credential_kinds.map(humanize).join(", ") : "None required"}
                  </p>
                </div>
                <div>
                  <p className="mb-1.5 text-xs font-medium text-muted-foreground">Configuration</p>
                  <ConfigFields schema={p.config_schema} />
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}

const KIND_LABEL = { self_managed: "Self-managed", managed: "Managed service", local: "Local development" };

export function DistributionCatalog() {
  const distributions = useDistributions();
  const providers = useProviders();
  const providerTypes = providers.data?.map((p) => p.type) ?? [];
  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow="Kubernetes"
        title="Kubernetes distributions"
        description="How Kubernetes is installed and operated. Distributions produce reviewable workflow plans that run on machines supplied by infrastructure providers."
      />
      <CatalogNotice what="Kubernetes distributions" />
      {distributions.isLoading && <Skeleton className="h-96" />}
      {distributions.isError && <ErrorState error={distributions.error} onRetry={() => distributions.refetch()} />}
      {distributions.data && (
        <Card className="py-0">
          <CardContent className="px-0">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Distribution</TableHead>
                  <TableHead>Model</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Runtime</TableHead>
                  <TableHead>CNI options</TableHead>
                  <TableHead>High availability</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {distributions.data.map((d) => (
                  <TableRow key={d.type}>
                    <TableCell className="max-w-72">
                      <p className="font-medium">{d.display_name}</p>
                      <p className="truncate text-xs text-muted-foreground" title={d.description}>
                        {d.description}
                      </p>
                    </TableCell>
                    <TableCell className="text-sm">{KIND_LABEL[d.kind]}</TableCell>
                    <TableCell>
                      <ImplementationBadge status={d.implementation_status} phase={d.planned_phase} />
                    </TableCell>
                    <TableCell className="font-mono text-xs">{d.supported_runtimes.join(", ")}</TableCell>
                    <TableCell className="font-mono text-xs">{d.supported_cnis.join(", ")}</TableCell>
                    <TableCell className="text-sm">{d.capabilities.includes("high_availability") ? "Yes" : "No"}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      )}
      {distributions.data && providerTypes.length > 0 && (
        <Card className="py-0">
          <CardHeader className="pt-4">
            <CardTitle className="text-sm">Infrastructure compatibility</CardTitle>
            <CardDescription>Which distributions can be installed on which provider</CardDescription>
          </CardHeader>
          <CardContent className="px-0">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Distribution</TableHead>
                  {providerTypes.map((p) => (
                    <TableHead key={p} className="text-center">
                      {humanize(p)}
                    </TableHead>
                  ))}
                </TableRow>
              </TableHeader>
              <TableBody>
                {distributions.data.map((d) => (
                  <TableRow key={d.type}>
                    <TableCell className="font-medium">{d.display_name}</TableCell>
                    {providerTypes.map((p) => (
                      <TableCell key={p} className="text-center">
                        {d.supported_providers.includes(p) ? (
                          <span className="text-emerald-400" aria-label="supported">●</span>
                        ) : (
                          <span className="text-muted-foreground/40" aria-label="not supported">–</span>
                        )}
                      </TableCell>
                    ))}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
