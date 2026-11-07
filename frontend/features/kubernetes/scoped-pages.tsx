"use client";

import { useRouter } from "next/navigation";

import { ErrorState, PageHeader } from "@/components/common/page";
import { Skeleton } from "@/components/ui/skeleton";
import { useCluster, useClusters } from "@/lib/api/hooks";
import { ClusterSettings } from "./cluster-panels";
import { EventsTable, NamespacesTable, NodesTable, TopologyView } from "./resources";
import { ClusterScope } from "./shared";

function ScopedHeader({ clusterId, eyebrow, title, description }: { clusterId: string; eyebrow: string; title: string; description: string }) {
  const clusters = useClusters();
  const name = clusters.data?.find((c) => c.id === clusterId)?.name;
  return <PageHeader eyebrow={eyebrow} title={name ? `${title} · ${name}` : title} description={description} />;
}

function Scoped({
  eyebrow,
  title,
  description,
  render,
}: {
  eyebrow: string;
  title: string;
  description: string;
  render: (clusterId: string) => React.ReactNode;
}) {
  return (
    <div className="space-y-6">
      <ClusterScope>
        {(id) => (
          <>
            <ScopedHeader clusterId={id} eyebrow={eyebrow} title={title} description={description} />
            {render(id)}
          </>
        )}
      </ClusterScope>
    </div>
  );
}

export const NodesPage = () => (
  <Scoped eyebrow="Kubernetes" title="Nodes" description="Live node status, capacity and utilization. Click a node for conditions, pods and events." render={(id) => <NodesTable clusterId={id} />} />
);

export const NamespacesPage = () => (
  <Scoped eyebrow="Kubernetes" title="Namespaces" description="Namespaces and the health of the pods they contain." render={(id) => <NamespacesTable clusterId={id} />} />
);

export const TopologyPage = () => (
  <Scoped eyebrow="Kubernetes" title="Topology" description="Where pods run: one square per pod, grouped by node and colored by status." render={(id) => <TopologyView clusterId={id} />} />
);

export const EventsPage = () => (
  <Scoped eyebrow="Operations" title="Events" description="Recent Kubernetes events (the API server keeps them for about an hour)." render={(id) => <EventsTable clusterId={id} />} />
);

function SettingsBody({ clusterId }: { clusterId: string }) {
  const router = useRouter();
  const cluster = useCluster(clusterId);
  if (cluster.isLoading) return <Skeleton className="h-64" />;
  if (cluster.isError || !cluster.data) return <ErrorState error={cluster.error} onRetry={() => cluster.refetch()} />;
  return <ClusterSettings cluster={cluster.data} onDelete={() => router.push(`/kubernetes/clusters/${clusterId}?tab=settings`)} />;
}

export const ClusterSettingsPage = () => (
  <Scoped eyebrow="Kubernetes" title="Cluster settings" description="Connection settings and lifecycle actions for the selected cluster." render={(id) => <SettingsBody clusterId={id} />} />
);
