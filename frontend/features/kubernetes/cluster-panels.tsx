"use client";

import { Activity, AlertTriangle, CheckCircle2, Gauge, PlugZap, RefreshCw, Save, Unplug } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { toast } from "sonner";

import { ConfirmDialog } from "@/components/common/confirm-dialog";
import { Section } from "@/components/common/page";
import { StatusPill } from "@/components/common/status";
import { useCan } from "@/components/providers/auth";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import type { ClusterDetail } from "@/lib/api/types";
import {
  useClusterHealth,
  useInstallMetricsServer,
  useReconnectCluster,
  useUnregisterCluster,
  useUpdateConnection,
} from "@/lib/api/kube";
import { formatDateTime, formatRelative } from "@/lib/format";
import { LiveError, UsageBar } from "./shared";

function Stat({ icon: Icon, label, value, detail, tone }: { icon: React.ComponentType<{ className?: string }>; label: string; value: React.ReactNode; detail?: React.ReactNode; tone?: "danger" | "warning" }) {
  return (
    <Card>
      <CardHeader>
        <CardDescription className="flex items-center gap-2"><Icon className="size-4" /> {label}</CardDescription>
        <CardTitle className={tone === "danger" ? "text-2xl text-red-400" : tone === "warning" ? "text-2xl text-amber-400" : "text-2xl"}>{value}</CardTitle>
        {detail && <p className="text-xs text-muted-foreground">{detail}</p>}
      </CardHeader>
    </Card>
  );
}

function MetricsServerDialog({ cluster, open, onOpenChange }: { cluster: ClusterDetail; open: boolean; onOpenChange: (open: boolean) => void }) {
  const router = useRouter();
  const install = useInstallMetricsServer(cluster.id);
  const [insecure, setInsecure] = useState(cluster.distribution === "kubeadm");
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Install metrics-server</DialogTitle>
          <DialogDescription>
            Installs metrics-server v0.9.1 (pinned upstream manifest) into kube-system so live CPU and memory usage is
            available. It runs as a regular workload in the cluster.
          </DialogDescription>
        </DialogHeader>
        <label className="flex items-start gap-3 text-sm">
          <input type="checkbox" className="mt-1" checked={insecure} onChange={(e) => setInsecure(e.target.checked)} />
          <span>
            <span className="font-medium">Skip kubelet certificate verification</span>
            <span className="block text-muted-foreground">
              Needed on kubeadm clusters whose kubelets use self-signed serving certificates (the default). metrics-server
              then trusts kubelets without verifying their identity inside the cluster network.
            </span>
          </span>
        </label>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button
            disabled={install.isPending}
            onClick={() =>
              install.mutate(insecure, {
                onSuccess: (op) => router.push(`/operations/${op.id}`),
                onError: (e) => toast.error("Could not start installation", { description: e.message }),
              })
            }
          >
            Install
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function ClusterOverview({ cluster }: { cluster: ClusterDetail }) {
  const canWrite = useCan("clusters.write");
  const health = useClusterHealth(cluster.id);
  const [metricsOpen, setMetricsOpen] = useState(false);
  if (health.isLoading) return <Skeleton className="h-72" />;
  if (health.isError) return <LiveError error={health.error} clusterId={cluster.id} onRetry={() => health.refetch()} />;
  const h = health.data!;
  const checks = h.readyz_checks ?? [];
  return (
    <div className="space-y-6">
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <Stat
          icon={PlugZap}
          label="API server"
          value={h.api_ready ? "Ready" : "Not ready"}
          tone={h.api_ready ? undefined : "danger"}
          detail={`${h.version} · ${checks.filter((c) => c.ok).length}/${checks.length} readiness checks`}
        />
        <Stat icon={CheckCircle2} label="Nodes ready" value={`${h.nodes.ready} / ${h.nodes.total}`} tone={h.nodes.not_ready.length ? "danger" : undefined} detail={h.nodes.not_ready.length ? `Not ready: ${h.nodes.not_ready.join(", ")}` : "All nodes ready"} />
        <Stat icon={Activity} label="Pods running" value={`${h.pods.running} / ${h.pods.total}`} detail={`${h.pods.pending} pending · ${h.pods.failed} failed · ${h.pods.restarts} restarts`} />
        <Stat icon={AlertTriangle} label="Failing pods" value={h.pods.problems} tone={h.pods.problems ? "warning" : undefined} detail="CrashLoopBackOff, image pull errors, …" />
      </div>
      <div className="grid gap-6 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Resource utilization</CardTitle>
            <CardDescription>Across all nodes (allocatable capacity)</CardDescription>
          </CardHeader>
          <CardContent className="space-y-5">
            <UsageBar label="CPU" kind="cpu" usage={h.cpu} />
            <UsageBar label="Memory" kind="memory" usage={h.memory} />
            {!h.metrics_available && (
              <div className="flex items-center justify-between gap-3 rounded-md border border-border p-3 text-sm">
                <span className="flex items-center gap-2 text-muted-foreground"><Gauge className="size-4" /> Live usage requires metrics-server.</span>
                <Button disabled={!canWrite} size="sm" variant="outline" onClick={() => setMetricsOpen(true)}>Install metrics-server</Button>
              </div>
            )}
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Alerts</CardTitle>
            <CardDescription>Derived from live cluster state</CardDescription>
          </CardHeader>
          <CardContent>
            {h.alerts.length === 0 ? (
              <p className="flex items-center gap-2 text-sm text-emerald-300"><CheckCircle2 className="size-4" /> No problems detected.</p>
            ) : (
              <ul className="space-y-2">
                {h.alerts.map((a) => (
                  <li key={a.message} className="flex gap-2 text-sm">
                    <StatusPill tone={a.severity === "critical" ? "danger" : "warning"}>{a.severity}</StatusPill>
                    <span>{a.message}</span>
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>
      </div>
      <MetricsServerDialog cluster={cluster} open={canWrite && metricsOpen} onOpenChange={setMetricsOpen} />
    </div>
  );
}

export function ClusterSettings({ cluster, onDelete }: { cluster: ClusterDetail; onDelete: () => void }) {
  const canWrite = useCan("clusters.write");
  const router = useRouter();
  const update = useUpdateConnection(cluster.id);
  const reconnect = useReconnectCluster(cluster.id);
  const unregister = useUnregisterCluster();
  const [override, setOverride] = useState(cluster.api_server_override ?? "");
  const [tlsName, setTlsName] = useState(cluster.tls_server_name ?? "");
  const [unregistering, setUnregistering] = useState(false);
  const dirty = override !== (cluster.api_server_override ?? "") || tlsName !== (cluster.tls_server_name ?? "");
  const goToOp = (op: { id: string }) => router.push(`/operations/${op.id}`);

  return (
    <div className="space-y-8">
      <Section title="Connection" description="How the platform reaches this cluster's Kubernetes API">
        <Card>
          <CardContent className="space-y-5">
            <dl className="grid gap-x-6 gap-y-2 text-sm md:grid-cols-2">
              <div className="flex justify-between gap-4"><dt className="text-muted-foreground">API server (kubeconfig)</dt><dd className="font-mono text-xs">{cluster.api_endpoint ?? "—"}</dd></div>
              <div className="flex justify-between gap-4"><dt className="text-muted-foreground">Context</dt><dd className="font-mono text-xs">{cluster.kubeconfig_context ?? (cluster.origin === "provisioned" ? "installer admin" : "—")}</dd></div>
              <div className="flex justify-between gap-4"><dt className="text-muted-foreground">Last contact</dt><dd>{cluster.last_contact_at ? formatRelative(cluster.last_contact_at) : "never"}</dd></div>
              <div className="flex justify-between gap-4"><dt className="text-muted-foreground">Credentials</dt><dd>{cluster.has_kubeconfig ? "stored encrypted" : "none"}</dd></div>
            </dl>
            {cluster.health_error && (
              <p className="rounded-md border border-amber-500/30 bg-amber-500/5 p-3 text-sm text-amber-200">
                Last connection attempt failed{cluster.health_error.observed_at ? ` (${formatDateTime(cluster.health_error.observed_at)})` : ""}: {cluster.health_error.message}
              </p>
            )}
            <div className="grid gap-4 md:grid-cols-2">
              <div className="space-y-1.5">
                <Label htmlFor="override">API server override</Label>
                <Input readOnly={!canWrite} id="override" value={override} onChange={(e) => setOverride(e.target.value)} placeholder="https://lb.example.com:6443" className="font-mono" />
                <p className="text-xs text-muted-foreground">Use when the platform must reach the API through a different address (NAT, proxy, load balancer).</p>
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="tls-name">TLS server name</Label>
                <Input readOnly={!canWrite} id="tls-name" value={tlsName} onChange={(e) => setTlsName(e.target.value)} placeholder="name or IP in the API server certificate" className="font-mono" />
                <p className="text-xs text-muted-foreground">The certificate is always verified; this names the identity to verify when connecting via the override.</p>
              </div>
            </div>
            <div className="flex gap-2">
              <Button
                size="sm"
                disabled={!canWrite || !dirty || update.isPending || !cluster.has_kubeconfig}
                onClick={() =>
                  update.mutate(
                    { server_override: override.trim() || null, tls_server_name: tlsName.trim() || null },
                    { onSuccess: goToOp, onError: (e) => toast.error("Could not update connection", { description: e.message }) },
                  )
                }
              >
                <Save /> Save and test
              </Button>
              <Button
                size="sm"
                variant="outline"
                disabled={!canWrite || reconnect.isPending || !cluster.has_kubeconfig}
                onClick={() => reconnect.mutate(undefined, { onSuccess: goToOp, onError: (e) => toast.error("Could not reconnect", { description: e.message }) })}
              >
                <RefreshCw /> Test connection
              </Button>
            </div>
          </CardContent>
        </Card>
      </Section>

      {canWrite && <Section title="Danger zone">
        <Card className="border-red-500/30">
          <CardContent className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            {cluster.origin === "imported" ? (
              <>
                <div className="text-sm">
                  <p className="font-medium">Unregister cluster</p>
                  <p className="text-muted-foreground">Forget this cluster and its stored kubeconfig. Nothing in the cluster changes.</p>
                </div>
                <Button variant="outline" size="sm" onClick={() => setUnregistering(true)}><Unplug className="text-red-400" /> Unregister</Button>
              </>
            ) : (
              <>
                <div className="text-sm">
                  <p className="font-medium">Delete cluster</p>
                  <p className="text-muted-foreground">Resets Kubernetes on every node and releases the machines.</p>
                </div>
                <Button variant="outline" size="sm" disabled={Boolean(cluster.active_operation_id)} onClick={onDelete}>Delete…</Button>
              </>
            )}
          </CardContent>
        </Card>
      </Section>}

      <ConfirmDialog
        open={canWrite && unregistering}
        onOpenChange={setUnregistering}
        title={`Unregister ${cluster.name}?`}
        description={<p>The platform forgets the cluster and deletes its stored kubeconfig. The cluster and its workloads are not touched; you can import it again later.</p>}
        confirmText={cluster.name}
        confirmLabel="Unregister"
        pending={unregister.isPending}
        onConfirm={() =>
          unregister.mutate(
            { id: cluster.id, confirm: cluster.name },
            {
              onSuccess: () => {
                toast.success(`Unregistered ${cluster.name}`);
                router.push("/kubernetes/clusters");
              },
              onError: (e) => toast.error("Could not unregister", { description: e.message }),
            },
          )
        }
      />
    </div>
  );
}
