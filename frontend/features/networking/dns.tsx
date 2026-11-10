"use client";

import { FileCode } from "lucide-react";
import Link from "next/link";

import { EmptyState, PageHeader, TableSkeleton } from "@/components/common/page";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { ClusterScope, LiveError } from "@/features/kubernetes/shared";
import { DnsLookupPanel } from "@/features/networking/dns-lookup";
import { useCluster } from "@/lib/api/hooks";
import { useResources } from "@/lib/api/workloads";

type Resource = Record<string, unknown> & { name: string; namespace: string | null };

function DnsInventory({ clusterId }: { clusterId: string }) {
  const cluster = useCluster(clusterId);
  const services = useResources(clusterId, "services", "");
  const slices = useResources(clusterId, "endpointslices", "");
  const dnsDeployments = useResources(clusterId, "deployments", "kube-system");
  const rows = (services.data?.items ?? []) as Resource[];
  const sliceRows = (slices.data?.items ?? []) as Resource[];
  const domain = cluster.data?.spec?.network?.dns_domain || "cluster.local";
  const backends = new Map<string, string[]>();
  for (const slice of sliceRows) {
    const service = String(slice.service ?? "");
    const namespace = slice.namespace ?? "default";
    const key = `${namespace}/${service}`;
    const addresses = (Array.isArray(slice.ready_addresses) ? slice.ready_addresses : []) as string[];
    backends.set(key, [...(backends.get(key) ?? []), ...addresses]);
  }
  const loading = cluster.isLoading || services.isLoading || slices.isLoading || dnsDeployments.isLoading;
  const error = cluster.error ?? services.error ?? slices.error ?? dnsDeployments.error;
  const name = cluster.data?.name;
  const dnsController = dnsDeployments.data?.items.find((deployment) => deployment.name === "coredns" || deployment.name === "kube-dns");

  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="Networking"
        title={name ? `DNS & service discovery · ${name}` : "DNS & service discovery"}
        description={`Service DNS names and published endpoints using the cluster domain ${domain}. Test actual DNS resolution with an in-cluster lookup below.`}
        actions={<Button asChild size="sm" variant="outline"><Link href="/kubernetes/apply"><FileCode /> Apply YAML</Link></Button>}
      />
      <DnsLookupPanel clusterId={clusterId} key={clusterId} />
      <div className="rounded-lg border border-sky-500/20 bg-sky-500/5 p-3 text-sm text-muted-foreground">
        Normal Services resolve to their ClusterIP. Headless Services publish ready EndpointSlice addresses. DNS resolution itself is provided by the cluster&apos;s DNS add-on.
      </div>
      {dnsDeployments.data && <p className="text-sm text-muted-foreground">DNS deployment: {dnsController ? `${dnsController.name} (${String(dnsController.ready ?? "unknown")} ready)` : "No CoreDNS or kube-dns Deployment found in kube-system."}</p>}
      {loading && <TableSkeleton rows={5} columns={5} />}
      {error && <LiveError error={error} clusterId={clusterId} />}
      {services.data && !rows.length && <EmptyState title="No Services" description="Create a Service to see its cluster DNS name here." />}
      {services.data && rows.length > 0 && (
        <Card className="py-0"><CardContent className="px-0">
          <Table>
            <TableHeader><TableRow><TableHead>Service</TableHead><TableHead>DNS name</TableHead><TableHead>Resolution target</TableHead><TableHead>Published endpoints</TableHead><TableHead>Ports</TableHead></TableRow></TableHeader>
            <TableBody>
              {rows.map((service) => {
                const namespace = service.namespace ?? "default";
                const dnsName = `${service.name}.${namespace}.svc.${domain}`;
                const clusterIp = String(service.cluster_ip ?? "");
                const endpointAddresses = backends.get(`${namespace}/${service.name}`) ?? [];
                const target = clusterIp === "None" ? "Headless" : clusterIp || String(service.external_name ?? "—");
                return (
                  <TableRow key={`${namespace}/${service.name}`}>
                    <TableCell><p className="font-medium">{service.name}</p><p className="text-xs text-muted-foreground">{namespace}</p></TableCell>
                    <TableCell className="font-mono text-xs">{dnsName}</TableCell>
                    <TableCell className="font-mono text-xs">{target}</TableCell>
                    <TableCell className="max-w-72 break-all font-mono text-xs">{endpointAddresses.length ? endpointAddresses.join(", ") : "—"}</TableCell>
                    <TableCell className="font-mono text-xs">{Array.isArray(service.ports) ? service.ports.join(", ") : "—"}</TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </CardContent></Card>
      )}
    </div>
  );
}

export function DnsPage() {
  return <ClusterScope>{(clusterId) => <DnsInventory clusterId={clusterId} />}</ClusterScope>;
}
