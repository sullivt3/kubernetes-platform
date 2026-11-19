"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Activity, ExternalLink, RefreshCw } from "lucide-react";
import { useState } from "react";

import { EmptyState, ErrorState, PageHeader, TableSkeleton } from "@/components/common/page";
import { useCan } from "@/components/providers/auth";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { ClusterScope, LiveError } from "@/features/kubernetes/shared";
import { api, apiRequest } from "@/lib/api/client";
import { formatBytes } from "@/lib/format";
import { TimeSeriesChart, type Series, type TimeRange } from "./chart";

type Connection = { namespace: string; service: string; port: string; grafana_url: string | null };
type Settings = { connection: Connection | null };
type Metric = "node_cpu" | "node_memory" | "pod_cpu" | "pod_memory" | "node_disk" | "node_disk_io" | "node_receive" | "node_transmit";
type Panel = { status: "ok" | "empty" | "unavailable" | "error"; series: Series[] };
type Report = TimeRange & { panels: Record<Metric, Panel> };

const windows = { "1h": "Last hour", "6h": "Last 6 hours", "24h": "Last 24 hours" } as const;
type Window = keyof typeof windows;
const percent = (value: number) => `${value.toFixed(1)}%`;
const cores = (value: number) => `${Math.round(value * 1000)}m`;
const metrics = {
  node_cpu: { title: "Node CPU", description: "Percent of CPU time in use", format: percent, ceiling: 100 },
  node_memory: { title: "Node memory", description: "Percent of memory in use", format: percent, ceiling: 100 },
  node_disk: { title: "Disk usage", description: "Filesystem space used", format: percent, ceiling: 100 },
  node_disk_io: { title: "Disk I/O", description: "Read and write throughput", format: (v: number) => formatBytes(v) + "/s", ceiling: 0 },
  node_receive: { title: "Network receive", description: "Incoming traffic", format: (v: number) => formatBytes(v) + "/s", ceiling: 0 },
  node_transmit: { title: "Network transmit", description: "Outgoing traffic", format: (v: number) => formatBytes(v) + "/s", ceiling: 0 },
  pod_cpu: { title: "Pod CPU", description: "CPU usage in millicores", format: cores, ceiling: 0 },
  pod_memory: { title: "Pod memory", description: "Memory working set", format: formatBytes, ceiling: 0 },
};

function ConnectionForm({ clusterId, connection, onClose }: {
  clusterId: string; connection: Connection | null; onClose: () => void;
}) {
  const [values, setValues] = useState(connection ?? { namespace: "", service: "", port: "9090", grafana_url: null });
  const queryClient = useQueryClient();
  const save = useMutation({
    mutationFn: (connection: Connection | null) =>
      api.put<Settings>(`/clusters/${clusterId}/monitoring/connection`, { connection }),
    onSuccess: (settings) => {
      queryClient.setQueryData(["monitoring", clusterId, "connection"], settings);
      void queryClient.invalidateQueries({ queryKey: ["monitoring", clusterId, "report"] });
      onClose();
    },
  });
  return (
    <Card>
      <CardHeader><CardTitle>Monitoring connection</CardTitle></CardHeader>
      <CardContent>
        <form className="space-y-4" onSubmit={(event) => { event.preventDefault(); save.mutate(values); }}>
          <p className="text-sm text-muted-foreground">Enter the Kubernetes Service that serves your Prometheus HTTP API.</p>
          <div className="grid gap-3 sm:grid-cols-3">
            <label className="space-y-1 text-sm">Namespace
              <Input required maxLength={63} value={values.namespace} placeholder="monitoring"
                onChange={(event) => setValues({ ...values, namespace: event.target.value.trim() })} />
            </label>
            <label className="space-y-1 text-sm">Service
              <Input required maxLength={63} value={values.service} placeholder="prometheus"
                onChange={(event) => setValues({ ...values, service: event.target.value.trim() })} />
            </label>
            <label className="space-y-1 text-sm">Service port
              <Input required maxLength={15} value={values.port}
                onChange={(event) => setValues({ ...values, port: event.target.value.trim() })} />
            </label>
          </div>
          <label className="block max-w-xl space-y-1 text-sm">Grafana base URL (optional)
            <Input type="url" maxLength={2083} value={values.grafana_url ?? ""} placeholder="https://grafana.example.com"
              onChange={(event) => setValues({ ...values, grafana_url: event.target.value.trim() || null })} />
          </label>
          {save.isError && <ErrorState error={save.error} />}
          <div className="flex flex-wrap gap-2">
            <Button disabled={save.isPending}>Save connection</Button>
            <Button type="button" variant="outline" onClick={onClose}>Cancel</Button>
            {connection && <Button type="button" variant="ghost" disabled={save.isPending} onClick={() => save.mutate(null)}>Disconnect</Button>}
          </div>
        </form>
      </CardContent>
    </Card>
  );
}

function GrafanaLinks({ baseUrl, window, namespace }: { baseUrl: string; window: Window; namespace: string }) {
  const base = baseUrl.replace(/\/+$/, "");
  const time = new URLSearchParams({ from: `now-${window}`, to: "now" });
  const pods = new URLSearchParams(time);
  if (namespace) pods.set("var-namespace", namespace);
  return (
    <div className="flex flex-wrap gap-2" aria-label="Grafana dashboards">
      <Button asChild variant="outline" size="sm">
        <a href={`${base}/d/kplatform-node-metrics/node-metrics?${time}`} target="_blank" rel="noopener noreferrer">
          <ExternalLink /> Node dashboard in Grafana
        </a>
      </Button>
      <Button asChild variant="outline" size="sm">
        <a href={`${base}/d/kplatform-pod-metrics/pod-metrics?${pods}`} target="_blank" rel="noopener noreferrer">
          <ExternalLink /> Pod dashboard in Grafana
        </a>
      </Button>
    </div>
  );
}

function Dashboard({ clusterId, connection }: { clusterId: string; connection: Connection }) {
  const [window, setWindow] = useState<Window>("1h");
  const [filter, setFilter] = useState("");
  const [namespace, setNamespace] = useState("");
  const report = useQuery({
    queryKey: ["monitoring", clusterId, "report", connection, window, namespace],
    queryFn: ({ signal }) => apiRequest<Report>(`/clusters/${clusterId}/monitoring`, {
      query: { window, namespace: namespace || undefined }, signal,
    }),
    refetchInterval: 60000,
    retry: false,
  });
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-3">
        <label className="space-y-1 text-sm">Time range
          <select className="block h-9 rounded-md border bg-background px-2" value={window}
            onChange={(event) => setWindow(event.target.value as Window)}>
            {Object.entries(windows).map(([value, text]) => <option key={value} value={value}>{text}</option>)}
          </select>
        </label>
        <form className="flex flex-wrap items-end gap-2" onSubmit={(event) => { event.preventDefault(); setNamespace(filter.trim()); }}>
          <label className="space-y-1 text-sm">Pod namespace
            <Input maxLength={63} placeholder="All namespaces" value={filter} onChange={(event) => setFilter(event.target.value)} />
          </label>
          <Button variant="outline">Apply filter</Button>
        </form>
        <Button variant="outline" className="ml-auto" disabled={report.isFetching} onClick={() => report.refetch()}>
          <RefreshCw /> Refresh
        </Button>
      </div>
      {connection.grafana_url && <GrafanaLinks baseUrl={connection.grafana_url} window={window} namespace={namespace} />}
      {report.isLoading && <TableSkeleton />}
      {report.isError && <LiveError error={report.error} clusterId={clusterId} onRetry={() => report.refetch()} />}
      {report.data && (
        <>
          <p className="text-xs text-muted-foreground">
            Up to eight series per chart. Samples every {report.data.step}s.
            {namespace && ` Pod charts show namespace ${namespace}.`}
            {report.isError && " Displaying the previous successful query."}
          </p>
          <div className="grid gap-4 xl:grid-cols-2">
            {(Object.keys(metrics) as Metric[]).map((key) => {
              const metric = metrics[key];
              const panel = report.data.panels[key];
              return (
                <Card key={key}>
                  <CardHeader>
                    <CardTitle>{metric.title}</CardTitle>
                    <p className="text-sm text-muted-foreground">{metric.description}</p>
                  </CardHeader>
                  <CardContent className="space-y-2">
                    {panel.status === "ok"
                      ? <TimeSeriesChart title={metric.title} series={panel.series} range={report.data}
                          format={metric.format} minimumCeiling={metric.ceiling} />
                      : <EmptyState icon={Activity} title={panel.status === "empty" ? "No samples in this range" : "Metric query failed"}
                          description={panel.status === "empty" ? "Check the scrape targets or select a different time range." : "Check the Prometheus connection and query health."} />}
                  </CardContent>
                </Card>
              );
            })}
          </div>
        </>
      )}
    </div>
  );
}

function ClusterMonitoring({ clusterId }: { clusterId: string }) {
  const canConfigure = useCan("clusters.write");
  const [editing, setEditing] = useState(false);
  const settings = useQuery({
    queryKey: ["monitoring", clusterId, "connection"],
    queryFn: () => api.get<Settings>(`/clusters/${clusterId}/monitoring/connection`),
  });
  if (settings.isError) return <LiveError error={settings.error} clusterId={clusterId} onRetry={() => settings.refetch()} />;
  if (!settings.data) return <TableSkeleton />;
  const { connection } = settings.data;
  return (
    <div className="space-y-4">
      {editing && canConfigure && <ConnectionForm clusterId={clusterId} connection={connection} onClose={() => setEditing(false)} />}
      {connection
        ? <>
            <div className="flex flex-wrap items-center justify-between gap-2">
              <p className="text-sm text-muted-foreground">Prometheus <code>{connection.namespace}/{connection.service}:{connection.port}</code></p>
              {canConfigure && !editing && <Button variant="outline" onClick={() => setEditing(true)}>Edit connection</Button>}
            </div>
            <Dashboard clusterId={clusterId} connection={connection} />
          </>
        : !editing && <EmptyState icon={Activity} title="Prometheus connection required"
            description="Connect a Prometheus Service to see historical metrics for this cluster."
            action={canConfigure && <Button onClick={() => setEditing(true)}>Connect Prometheus</Button>} />}
    </div>
  );
}

export function MonitoringPage() {
  return (
    <div className="space-y-5">
      <PageHeader eyebrow="Operations" title="Monitoring" description="Historical node and pod metrics from Prometheus." />
      <ClusterScope>{(clusterId) => <ClusterMonitoring key={clusterId} clusterId={clusterId} />}</ClusterScope>
    </div>
  );
}
