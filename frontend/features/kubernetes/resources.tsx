"use client";

import { AlertTriangle, Plus } from "lucide-react";
import { useState } from "react";

import { EmptyState, TableSkeleton } from "@/components/common/page";
import { StatusPill } from "@/components/common/status";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { useCluster } from "@/lib/api/hooks";
import {
  useClusterEvents,
  useClusterNamespaces,
  useClusterNode,
  useClusterNodes,
  useClusterPods,
  useClusterTopology,
  type KubeEvent,
  type KubePod,
} from "@/lib/api/kube";
import { formatAge, formatBytes, formatCpu } from "@/lib/format";
import { cn } from "@/lib/utils";
import { AddNodesDialog, NodeActions } from "./node-actions";
import { LiveError, NodeReadyBadge, PodStatusBadge, UsageBar } from "./shared";

function Frame({ children }: { children: React.ReactNode }) {
  return (
    <Card className="py-0">
      <CardContent className="px-0">{children}</CardContent>
    </Card>
  );
}

function Pct({ value, total }: { value: number | null; total: number }) {
  if (value === null || !total) return <span className="text-muted-foreground">—</span>;
  const pct = Math.round((value / total) * 100);
  return <span className={cn("tabular-nums", pct > 85 ? "text-red-400" : pct > 70 ? "text-amber-400" : "")}>{pct}%</span>;
}

// ---------------------------------------------------------------------------- nodes

function NodeDialog({ clusterId, name, onClose }: { clusterId: string; name: string | null; onClose: () => void }) {
  const node = useClusterNode(clusterId, name);
  const cluster = useCluster(clusterId);
  const n = node.data;
  return (
    <Dialog open={name !== null} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-3xl">
        <DialogHeader>
          <DialogTitle>Node {name}</DialogTitle>
          <DialogDescription>Live from the Kubernetes API</DialogDescription>
        </DialogHeader>
        {node.isLoading && <Skeleton className="h-64" />}
        {node.isError && <LiveError error={node.error} clusterId={clusterId} onRetry={() => node.refetch()} />}
        {n && (
          <div className="min-w-0 space-y-5 text-sm">
            <div className="flex flex-wrap items-center gap-2">
              <NodeReadyBadge ready={n.ready} status={n.status} />
              {n.roles.map((r) => <Badge key={r} variant="secondary">{r}</Badge>)}
              {n.problems.map((p) => <StatusPill key={p} tone="danger">{p}</StatusPill>)}
            </div>
            <NodeActions clusterId={clusterId} node={n} cluster={cluster.data} />
            <div className="grid gap-4 md:grid-cols-2">
              <UsageBar label="CPU" kind="cpu" usage={{ allocatable_m: n.cpu.allocatable_m, requested_m: n.cpu.requested_m, used_m: n.cpu.used_m }} />
              <UsageBar label="Memory" kind="memory" usage={{ allocatable_bytes: n.memory.allocatable_bytes, requested_bytes: n.memory.requested_bytes, used_bytes: n.memory.used_bytes }} />
            </div>
            <dl className="grid grid-cols-[9rem_1fr] gap-x-3 gap-y-1">
              {(
                [
                  ["Internal IP", n.internal_ip],
                  ["Kubelet", n.kubelet_version],
                  ["OS", `${n.os_image} (${n.architecture})`],
                  ["Kernel", n.kernel],
                  ["Runtime", n.container_runtime],
                  ["Pods", `${n.pods.count} / ${n.pods.capacity}`],
                  ["Age", formatAge(n.created)],
                ] as const
              ).map(([k, v]) => (
                <div key={k} className="contents">
                  <dt className="text-muted-foreground">{k}</dt>
                  <dd className="font-mono text-xs leading-5">{v ?? "—"}</dd>
                </div>
              ))}
            </dl>
            <div>
              <p className="mb-1 font-medium">Conditions</p>
              <ul className="space-y-1">
                {n.conditions.map((c) => {
                  const healthy = c.type === "Ready" ? c.status === "True" : c.status === "False";
                  return (
                    <li key={c.type} className="flex gap-2 text-xs">
                      <StatusPill tone={healthy ? "success" : "danger"}>{c.type}={c.status}</StatusPill>
                      <span className="text-muted-foreground">{c.message}</span>
                    </li>
                  );
                })}
              </ul>
            </div>
            {n.taints.length > 0 && (
              <div>
                <p className="mb-1 font-medium">Taints</p>
                <div className="flex flex-wrap gap-1">
                  {n.taints.map((t) => <span key={`${t.key}${t.effect}`} className="rounded bg-muted px-1.5 font-mono text-[11px]">{t.key}{t.value ? `=${t.value}` : ""}:{t.effect}</span>)}
                </div>
              </div>
            )}
            <div>
              <p className="mb-1 font-medium">Labels</p>
              <div className="flex flex-wrap gap-1">
                {Object.entries(n.labels).map(([k, v]) => <span key={k} className="rounded bg-muted px-1.5 font-mono text-[11px]">{k}{v ? `=${v}` : ""}</span>)}
              </div>
            </div>
            <div>
              <p className="mb-1 font-medium">Pods on this node ({n.pod_list.length})</p>
              <PodRows pods={n.pod_list} showNode={false} />
            </div>
            <div>
              <p className="mb-1 font-medium">Events</p>
              {n.events.length ? <EventRows events={n.events} /> : <p className="text-xs text-muted-foreground">No recent events.</p>}
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

export function NodesTable({ clusterId }: { clusterId: string }) {
  const nodes = useClusterNodes(clusterId);
  const cluster = useCluster(clusterId);
  const [open, setOpen] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  if (nodes.isLoading) return <TableSkeleton rows={3} columns={7} />;
  if (nodes.isError) return <LiveError error={nodes.error} clusterId={clusterId} onRetry={() => nodes.refetch()} />;
  const c = cluster.data;
  const canAdd = c?.origin === "provisioned" && (c.lifecycle_state === "active" || c.lifecycle_state === "degraded");
  return (
    <>
      {c?.origin === "provisioned" && (
        <div className="mb-3 flex items-center justify-between gap-3">
          <p className="text-sm text-muted-foreground">
            {c.nodes.filter((m) => m.membership_state !== "joined").map((m) => `${m.node_name ?? m.machine_name}: ${m.membership_state}`).join(" · ") ||
              "Click a node to cordon, drain, label, taint, read its logs or remove it."}
          </p>
          <Button size="sm" onClick={() => setAdding(true)} disabled={!canAdd}><Plus /> Add nodes</Button>
        </div>
      )}
      <Frame>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Node</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>Roles</TableHead>
              <TableHead>Version</TableHead>
              <TableHead>Internal IP</TableHead>
              <TableHead className="text-right">CPU</TableHead>
              <TableHead className="text-right">Memory</TableHead>
              <TableHead className="text-right">Pods</TableHead>
              <TableHead className="text-right">Age</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {nodes.data?.map((n) => (
              <TableRow key={n.name} className="cursor-pointer" onClick={() => setOpen(n.name)}>
                <TableCell className="font-medium">
                  {n.name}
                  {n.problems.length > 0 && <AlertTriangle className="ml-1 inline size-3.5 text-amber-400" />}
                </TableCell>
                <TableCell><NodeReadyBadge ready={n.ready} status={n.status} /></TableCell>
                <TableCell className="text-sm">{n.roles.join(", ")}</TableCell>
                <TableCell className="font-mono text-xs">{n.kubelet_version}</TableCell>
                <TableCell className="font-mono text-xs">{n.internal_ip ?? "—"}</TableCell>
                <TableCell className="text-right text-sm" title={`requested ${formatCpu(n.cpu.requested_m)} of ${formatCpu(n.cpu.allocatable_m)}`}>
                  <Pct value={n.cpu.used_m ?? n.cpu.requested_m} total={n.cpu.allocatable_m} />
                  <span className="ml-1 text-xs text-muted-foreground">{n.cpu.used_m === null ? "req" : "used"}</span>
                </TableCell>
                <TableCell className="text-right text-sm" title={`requested ${formatBytes(n.memory.requested_bytes)} of ${formatBytes(n.memory.allocatable_bytes)}`}>
                  <Pct value={n.memory.used_bytes ?? n.memory.requested_bytes} total={n.memory.allocatable_bytes} />
                  <span className="ml-1 text-xs text-muted-foreground">{n.memory.used_bytes === null ? "req" : "used"}</span>
                </TableCell>
                <TableCell className="text-right tabular-nums text-sm">{n.pods.count}/{n.pods.capacity}</TableCell>
                <TableCell className="text-right text-sm text-muted-foreground">{formatAge(n.created)}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </Frame>
      <NodeDialog clusterId={clusterId} name={open} onClose={() => setOpen(null)} />
      {adding && c && <AddNodesDialog cluster={c} open onClose={() => setAdding(false)} />}
    </>
  );
}

// ---------------------------------------------------------------------------- pods

function PodRows({ pods, showNode = true }: { pods: KubePod[]; showNode?: boolean }) {
  if (!pods.length) return <EmptyState className="m-4" title="No pods" description="Nothing matches the current filters." />;
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Pod</TableHead>
          <TableHead>Status</TableHead>
          <TableHead>Ready</TableHead>
          <TableHead className="text-right">Restarts</TableHead>
          {showNode && <TableHead>Node</TableHead>}
          <TableHead>Owner</TableHead>
          <TableHead className="text-right">Age</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {pods.map((p) => (
          <TableRow key={`${p.namespace}/${p.name}`}>
            <TableCell className="max-w-80">
              <p className="truncate font-medium" title={p.name}>{p.name}</p>
              <p className="text-xs text-muted-foreground">{p.namespace}{p.pod_ip ? ` · ${p.pod_ip}` : ""}</p>
            </TableCell>
            <TableCell><PodStatusBadge status={p.status} problem={p.problem} phase={p.phase} /></TableCell>
            <TableCell className="tabular-nums text-sm">{p.ready}</TableCell>
            <TableCell className={cn("text-right tabular-nums text-sm", p.restarts > 5 && "text-amber-400")}>{p.restarts}</TableCell>
            {showNode && <TableCell className="text-sm">{p.node ?? <span className="text-muted-foreground">unscheduled</span>}</TableCell>}
            <TableCell className="text-xs text-muted-foreground">{p.owner_kind ? `${p.owner_kind}/${p.owner_name}` : "—"}</TableCell>
            <TableCell className="text-right text-sm text-muted-foreground">{formatAge(p.created)}</TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

export function PodsTable({ clusterId }: { clusterId: string }) {
  const [namespace, setNamespace] = useState("");
  const [problems, setProblems] = useState(false);
  const namespaces = useClusterNamespaces(clusterId);
  const pods = useClusterPods(clusterId, { namespace, problems });
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-3">
        <Select value={namespace || "__all"} onValueChange={(v) => setNamespace(v === "__all" ? "" : v)}>
          <SelectTrigger className="w-56" aria-label="Namespace"><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="__all">All namespaces</SelectItem>
            {namespaces.data?.map((n) => <SelectItem key={n.name} value={n.name}>{n.name}</SelectItem>)}
          </SelectContent>
        </Select>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={problems} onChange={(e) => setProblems(e.target.checked)} />
          Only failing or pending
        </label>
        {pods.data && <span className="ml-auto text-sm text-muted-foreground">{pods.data.items.length} pod(s){pods.data.truncated ? " (truncated)" : ""}</span>}
      </div>
      {pods.isLoading && <TableSkeleton rows={6} columns={6} />}
      {pods.isError && <LiveError error={pods.error} clusterId={clusterId} onRetry={() => pods.refetch()} />}
      {pods.data && <Frame><PodRows pods={pods.data.items} /></Frame>}
    </div>
  );
}

// ---------------------------------------------------------------------------- namespaces

export function NamespacesTable({ clusterId }: { clusterId: string }) {
  const namespaces = useClusterNamespaces(clusterId);
  if (namespaces.isLoading) return <TableSkeleton rows={4} columns={4} />;
  if (namespaces.isError) return <LiveError error={namespaces.error} clusterId={clusterId} onRetry={() => namespaces.refetch()} />;
  return (
    <Frame>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Namespace</TableHead>
            <TableHead>Status</TableHead>
            <TableHead className="text-right">Pods</TableHead>
            <TableHead className="text-right">Running</TableHead>
            <TableHead className="text-right">Failing</TableHead>
            <TableHead className="text-right">Age</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {namespaces.data?.map((n) => (
            <TableRow key={n.name}>
              <TableCell className="font-medium">{n.name}</TableCell>
              <TableCell><StatusPill tone={n.status === "Active" ? "success" : "warning"}>{n.status}</StatusPill></TableCell>
              <TableCell className="text-right tabular-nums">{n.pods.total}</TableCell>
              <TableCell className="text-right tabular-nums">{n.pods.running ?? 0}</TableCell>
              <TableCell className={cn("text-right tabular-nums", n.problem_pods > 0 && "text-red-400")}>{n.problem_pods}</TableCell>
              <TableCell className="text-right text-sm text-muted-foreground">{formatAge(n.created)}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </Frame>
  );
}

// ---------------------------------------------------------------------------- events

function EventRows({ events }: { events: KubeEvent[] }) {
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Type</TableHead>
          <TableHead>Reason</TableHead>
          <TableHead>Object</TableHead>
          <TableHead>Message</TableHead>
          <TableHead className="text-right">Count</TableHead>
          <TableHead className="text-right">Last seen</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {events.map((e, i) => (
          <TableRow key={`${e.object}-${e.reason}-${i}`}>
            <TableCell><StatusPill tone={e.type === "Warning" ? "warning" : "neutral"}>{e.type}</StatusPill></TableCell>
            <TableCell className="text-sm font-medium">{e.reason}</TableCell>
            <TableCell className="text-xs">
              {e.namespace && <span className="text-muted-foreground">{e.namespace}/</span>}
              {e.object}
            </TableCell>
            <TableCell className="max-w-md whitespace-normal text-xs text-muted-foreground">{e.message}</TableCell>
            <TableCell className="text-right tabular-nums text-sm">{e.count}</TableCell>
            <TableCell className="text-right text-sm text-muted-foreground">{formatAge(e.last_seen)}</TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

export function EventsTable({ clusterId }: { clusterId: string }) {
  const [warnings, setWarnings] = useState(false);
  const events = useClusterEvents(clusterId, { warnings });
  return (
    <div className="space-y-3">
      <label className="flex items-center gap-2 text-sm">
        <input type="checkbox" checked={warnings} onChange={(e) => setWarnings(e.target.checked)} />
        Warnings only
      </label>
      {events.isLoading && <TableSkeleton rows={6} columns={5} />}
      {events.isError && <LiveError error={events.error} clusterId={clusterId} onRetry={() => events.refetch()} />}
      {events.data && (events.data.length ? <Frame><EventRows events={events.data} /></Frame> : <EmptyState title="No events" description="Kubernetes keeps events for about an hour by default." />)}
    </div>
  );
}

// ---------------------------------------------------------------------------- topology

export function TopologyView({ clusterId }: { clusterId: string }) {
  const topology = useClusterTopology(clusterId);
  if (topology.isLoading) return <Skeleton className="h-80" />;
  if (topology.isError) return <LiveError error={topology.error} clusterId={clusterId} onRetry={() => topology.refetch()} />;
  const data = topology.data!;
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap gap-4 text-xs text-muted-foreground">
        <span className="flex items-center gap-1.5"><span className="size-2.5 rounded-sm bg-emerald-500/70" /> running</span>
        <span className="flex items-center gap-1.5"><span className="size-2.5 rounded-sm bg-amber-500/70" /> pending</span>
        <span className="flex items-center gap-1.5"><span className="size-2.5 rounded-sm bg-red-500/80" /> failing</span>
        <span className="flex items-center gap-1.5"><span className="size-2.5 rounded-sm bg-muted-foreground/40" /> completed / other</span>
      </div>
      <div className="grid gap-4 lg:grid-cols-2">
        {data.nodes.map((n) => (
          <Card key={n.name}>
            <CardContent className="space-y-3">
              <div className="flex items-center justify-between gap-2">
                <div>
                  <p className="font-medium">{n.name}</p>
                  <p className="text-xs text-muted-foreground">{n.roles.join(", ")} · {n.internal_ip} · {n.pods.count}/{n.pods.capacity} pods</p>
                </div>
                <NodeReadyBadge ready={n.ready} status={n.status} />
              </div>
              <div className="flex flex-wrap gap-1">
                {n.pod_list.map((p) => (
                  <span
                    key={`${p.namespace}/${p.name}`}
                    title={`${p.namespace}/${p.name} — ${p.status}`}
                    className={cn(
                      "size-4 rounded-sm",
                      p.problem || p.phase === "Failed" ? "bg-red-500/80" : p.phase === "Running" ? "bg-emerald-500/70" : p.phase === "Pending" ? "bg-amber-500/70" : "bg-muted-foreground/40",
                    )}
                  />
                ))}
              </div>
              <div className="flex flex-wrap gap-1.5">
                {Array.from(new Set(n.pod_list.map((p) => p.namespace))).sort().map((ns) => (
                  <Badge key={ns} variant="outline" className="font-normal">{ns}: {n.pod_list.filter((p) => p.namespace === ns).length}</Badge>
                ))}
              </div>
            </CardContent>
          </Card>
        ))}
      </div>
      {data.unscheduled.length > 0 && (
        <div className="rounded-lg border border-amber-500/30 bg-amber-500/5 p-3 text-sm">
          <p className="font-medium text-amber-200">{data.unscheduled.length} pod(s) not scheduled to any node</p>
          <p className="text-xs text-muted-foreground">{data.unscheduled.map((p) => `${p.namespace}/${p.name}`).join(", ")}</p>
        </div>
      )}
    </div>
  );
}
