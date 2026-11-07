"use client";

import { Ban, CircleSlash, FileText, Plus, Rocket, ScrollText, Tag, Trash2, Waves, X } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";

import { ConfirmDialog } from "@/components/common/confirm-dialog";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ApiError } from "@/lib/api/client";
import { useCredentials, useMachines } from "@/lib/api/hooks";
import { useConnections } from "@/lib/api/infra";
import type { KubeNode } from "@/lib/api/kube";
import {
  useAddNodes,
  useCordonNode,
  useDrainNode,
  useNodeLogs,
  useRemoveNode,
  useReplaceNodeTaints,
  useUpdateNodeLabels,
} from "@/lib/api/nodes";
import type { ClusterDetail, ClusterNode, Taint } from "@/lib/api/types";
import { cn } from "@/lib/utils";

const PROTECTED_LABEL = /^(kubernetes\.io\/(hostname|os|arch)|beta\.kubernetes\.io\/.*|node\.kubernetes\.io\/(instance-type|windows-build)|topology\.kubernetes\.io\/.*|failure-domain\.beta\.kubernetes\.io\/.*)$/;
const SYSTEM_TAINT = /^node\.(kubernetes|cloudprovider\.kubernetes)\.io\//;
const DNS_LABEL = /^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$/;

function errorText(e: unknown): string {
  if (e instanceof ApiError) {
    const problems = e.details?.problems as string[] | undefined;
    return problems?.length ? `${e.message}: ${problems.join("; ")}` : e.message;
  }
  return String(e);
}

function useOperationToast() {
  const router = useRouter();
  return (title: string, operationId: string) =>
    toast.info(title, { action: { label: "View progress", onClick: () => router.push(`/operations/${operationId}`) } });
}

// ---- labels ---------------------------------------------------------------------------------

function LabelsDialog({ clusterId, node, open, onClose }: { clusterId: string; node: KubeNode; open: boolean; onClose: () => void }) {
  const update = useUpdateNodeLabels(clusterId);
  const editable = Object.entries(node.labels).filter(([k]) => !PROTECTED_LABEL.test(k));
  const [rows, setRows] = useState(editable.map(([key, value]) => ({ key, value })));
  const [error, setError] = useState<string | null>(null);
  const save = () => {
    const desired = Object.fromEntries(rows.filter((r) => r.key.trim()).map((r) => [r.key.trim(), r.value.trim()]));
    const set = Object.fromEntries(Object.entries(desired).filter(([k, v]) => node.labels[k] !== v));
    const remove = editable.map(([k]) => k).filter((k) => !(k in desired));
    if (!Object.keys(set).length && !remove.length) return onClose();
    update.mutate(
      { name: node.name, set, remove },
      { onSuccess: () => { toast.success(`Labels of ${node.name} updated`); onClose(); }, onError: (e) => setError(errorText(e)) },
    );
  };
  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>Labels of {node.name}</DialogTitle>
          <DialogDescription>Used by node selectors and affinity rules. Labels Kubernetes maintains are read-only.</DialogDescription>
        </DialogHeader>
        <div className="space-y-2">
          {Object.entries(node.labels).filter(([k]) => PROTECTED_LABEL.test(k)).map(([k, v]) => (
            <p key={k} className="font-mono text-xs text-muted-foreground">{k}={v}</p>
          ))}
          {rows.map((row, i) => (
            <div key={i} className="flex gap-2">
              <Input value={row.key} placeholder="key" className="font-mono text-xs" onChange={(e) => setRows(rows.map((r, j) => (j === i ? { ...r, key: e.target.value } : r)))} />
              <Input value={row.value} placeholder="value" className="font-mono text-xs" onChange={(e) => setRows(rows.map((r, j) => (j === i ? { ...r, value: e.target.value } : r)))} />
              <Button variant="ghost" size="icon" aria-label="Remove label" onClick={() => setRows(rows.filter((_, j) => j !== i))}><X /></Button>
            </div>
          ))}
          <Button variant="ghost" size="xs" onClick={() => setRows([...rows, { key: "", value: "" }])}><Plus /> Add label</Button>
        </div>
        {error && <p role="alert" className="rounded border border-red-500/30 bg-red-500/5 p-2 text-sm text-red-300">{error}</p>}
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>Cancel</Button>
          <Button onClick={save} disabled={update.isPending}>{update.isPending ? "Saving…" : "Save labels"}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ---- taints ---------------------------------------------------------------------------------

function TaintsDialog({ clusterId, node, open, onClose }: { clusterId: string; node: KubeNode; open: boolean; onClose: () => void }) {
  const replace = useReplaceNodeTaints(clusterId);
  const system = node.taints.filter((t) => SYSTEM_TAINT.test(t.key));
  const [rows, setRows] = useState<Taint[]>(
    node.taints.filter((t) => !SYSTEM_TAINT.test(t.key)).map((t) => ({ key: t.key, value: t.value ?? "", effect: t.effect as Taint["effect"] })),
  );
  const [error, setError] = useState<string | null>(null);
  const save = () =>
    replace.mutate(
      { name: node.name, taints: rows.filter((t) => t.key.trim()).map((t) => ({ ...t, key: t.key.trim(), value: t.value?.trim() || null })) },
      { onSuccess: () => { toast.success(`Taints of ${node.name} updated`); onClose(); }, onError: (e) => setError(errorText(e)) },
    );
  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>Taints of {node.name}</DialogTitle>
          <DialogDescription>
            Pods are only scheduled here if they tolerate every NoSchedule taint; NoExecute also evicts running pods
            that do not tolerate it.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-2">
          {system.map((t) => (
            <p key={`${t.key}${t.effect}`} className="font-mono text-xs text-muted-foreground">
              {t.key}:{t.effect} (managed by Kubernetes)
            </p>
          ))}
          {rows.map((t, i) => (
            <div key={i} className="flex gap-2">
              <Input value={t.key} placeholder="key" className="font-mono text-xs" onChange={(e) => setRows(rows.map((x, j) => (j === i ? { ...x, key: e.target.value } : x)))} />
              <Input value={t.value ?? ""} placeholder="value (optional)" className="font-mono text-xs" onChange={(e) => setRows(rows.map((x, j) => (j === i ? { ...x, value: e.target.value } : x)))} />
              <Select value={t.effect} onValueChange={(v) => setRows(rows.map((x, j) => (j === i ? { ...x, effect: v as Taint["effect"] } : x)))}>
                <SelectTrigger className="w-44"><SelectValue /></SelectTrigger>
                <SelectContent>
                  {(["NoSchedule", "PreferNoSchedule", "NoExecute"] as const).map((e) => <SelectItem key={e} value={e}>{e}</SelectItem>)}
                </SelectContent>
              </Select>
              <Button variant="ghost" size="icon" aria-label="Remove taint" onClick={() => setRows(rows.filter((_, j) => j !== i))}><X /></Button>
            </div>
          ))}
          <Button variant="ghost" size="xs" onClick={() => setRows([...rows, { key: "", value: "", effect: "NoSchedule" }])}><Plus /> Add taint</Button>
        </div>
        {error && <p role="alert" className="rounded border border-red-500/30 bg-red-500/5 p-2 text-sm text-red-300">{error}</p>}
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>Cancel</Button>
          <Button onClick={save} disabled={replace.isPending}>{replace.isPending ? "Saving…" : "Save taints"}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ---- drain ----------------------------------------------------------------------------------

function DrainDialog({ clusterId, node, open, onClose }: { clusterId: string; node: KubeNode; open: boolean; onClose: () => void }) {
  const drain = useDrainNode(clusterId);
  const notify = useOperationToast();
  const [force, setForce] = useState(false);
  const [emptyDir, setEmptyDir] = useState(false);
  const [timeout, setTimeoutSeconds] = useState("600");
  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Drain {node.name}?</DialogTitle>
          <DialogDescription>
            The node is cordoned and its pods are evicted so their controllers recreate them elsewhere.
            PodDisruptionBudgets are respected; DaemonSet and static pods stay.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3 text-sm">
          <label className="flex items-start gap-2">
            <input type="checkbox" className="mt-1" checked={emptyDir} onChange={(e) => setEmptyDir(e.target.checked)} />
            <span>Evict pods that use emptyDir volumes <span className="block text-xs text-muted-foreground">Their scratch data is lost.</span></span>
          </label>
          <label className="flex items-start gap-2">
            <input type="checkbox" className="mt-1" checked={force} onChange={(e) => setForce(e.target.checked)} />
            <span>Also delete pods no controller manages <span className="block text-xs text-muted-foreground">They are not recreated anywhere.</span></span>
          </label>
          <div className="flex items-center gap-2">
            <Label htmlFor="drain-timeout">Give up after</Label>
            <Input id="drain-timeout" value={timeout} onChange={(e) => setTimeoutSeconds(e.target.value)} className="w-24" inputMode="numeric" />
            <span className="text-muted-foreground">seconds</span>
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>Cancel</Button>
          <Button
            disabled={drain.isPending || !(Number(timeout) >= 30)}
            onClick={() =>
              drain.mutate(
                { name: node.name, force, delete_emptydir_data: emptyDir, timeout_seconds: Number(timeout) },
                { onSuccess: (op) => { notify(`Draining ${node.name}`, op.id); onClose(); }, onError: (e) => toast.error("Could not start drain", { description: errorText(e) }) },
              )
            }
          >
            {drain.isPending ? "Starting…" : "Drain node"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ---- logs -----------------------------------------------------------------------------------

export function NodeLogs({ clusterId, name }: { clusterId: string; name: string }) {
  const [unit, setUnit] = useState<"kubelet" | "containerd">("kubelet");
  const logs = useNodeLogs(clusterId, name, unit, true);
  const view = useRef<HTMLPreElement>(null);
  useEffect(() => {
    // journalctl lists oldest first: start at the newest lines.
    if (view.current) view.current.scrollTop = view.current.scrollHeight;
  }, [logs.data]);
  return (
    <div className="min-w-0 space-y-2">
      <div className="flex items-center justify-between gap-2">
        <Tabs value={unit} onValueChange={(v) => setUnit(v as "kubelet" | "containerd")}>
          <TabsList>
            <TabsTrigger value="kubelet">kubelet</TabsTrigger>
            <TabsTrigger value="containerd">containerd</TabsTrigger>
          </TabsList>
        </Tabs>
        <Button size="xs" variant="ghost" onClick={() => logs.refetch()} disabled={logs.isFetching}>Refresh</Button>
      </div>
      {logs.isLoading && <Skeleton className="h-48" />}
      {logs.isError && <p className="rounded border border-red-500/30 bg-red-500/5 p-2 text-sm text-red-300">{errorText(logs.error)}</p>}
      {logs.data !== undefined && (
        <pre
          ref={view}
          className="max-h-80 overflow-auto rounded-md border border-border bg-black/40 p-3 font-mono text-[11px] leading-4 break-all whitespace-pre-wrap"
        >
          {logs.data || "No log lines."}
        </pre>
      )}
      <p className="text-xs text-muted-foreground">Last 400 lines from the node&apos;s journal, read over SSH.</p>
    </div>
  );
}

// ---- remove ---------------------------------------------------------------------------------

function RemoveNodeDialog({ clusterId, member, open, onClose }: { clusterId: string; member: ClusterNode; open: boolean; onClose: () => void }) {
  const remove = useRemoveNode(clusterId);
  const notify = useOperationToast();
  const name = member.node_name ?? member.machine_name;
  const [destroy, setDestroy] = useState(false);
  const [force, setForce] = useState(false);
  return (
    <ConfirmDialog
      open={open}
      onOpenChange={(o) => !o && onClose()}
      title={`Remove ${name} from the cluster?`}
      description={
        <div className="space-y-3">
          <p>
            Its workloads are drained to other nodes, Kubernetes is reset on it
            {member.role === "control_plane" ? ", it leaves etcd" : ""} and the machine returns to the inventory.
          </p>
          {member.provisioned && (
            <label className="flex items-start gap-2 text-sm">
              <input type="checkbox" className="mt-0.5" checked={destroy} onChange={(e) => setDestroy(e.target.checked)} />
              Also destroy the machine (the platform created it)
            </label>
          )}
          <label className="flex items-start gap-2 text-sm">
            <input type="checkbox" className="mt-0.5" checked={force} onChange={(e) => setForce(e.target.checked)} />
            Continue if the node is unreachable or cannot be drained
          </label>
        </div>
      }
      confirmText={name}
      confirmLabel="Remove node"
      pending={remove.isPending}
      onConfirm={() =>
        remove.mutate(
          { name, destroyMachine: destroy, force },
          { onSuccess: (op) => { notify(`Removing ${name}`, op.id); onClose(); }, onError: (e) => toast.error("Could not remove node", { description: errorText(e) }) },
        )
      }
    />
  );
}

// ---- action bar -----------------------------------------------------------------------------

export function NodeActions({ clusterId, node, cluster }: { clusterId: string; node: KubeNode; cluster?: ClusterDetail }) {
  const cordon = useCordonNode(clusterId);
  const [dialog, setDialog] = useState<"labels" | "taints" | "drain" | "remove" | null>(null);
  const [showLogs, setShowLogs] = useState(false);
  const member = cluster?.nodes.find((n) => n.node_name === node.name);
  const managed = cluster?.origin === "provisioned" && Boolean(member);
  return (
    <div className="min-w-0 space-y-3">
      <div className="flex flex-wrap gap-2">
        <Button
          size="sm"
          variant="outline"
          disabled={cordon.isPending}
          onClick={() =>
            cordon.mutate(
              { name: node.name, cordon: !node.unschedulable },
              {
                onSuccess: () => toast.success(node.unschedulable ? `${node.name} accepts new pods again` : `${node.name} cordoned`),
                onError: (e) => toast.error("Could not change scheduling", { description: errorText(e) }),
              },
            )
          }
        >
          {node.unschedulable ? <CircleSlash /> : <Ban />} {node.unschedulable ? "Uncordon" : "Cordon"}
        </Button>
        <Button size="sm" variant="outline" onClick={() => setDialog("drain")}><Waves /> Drain…</Button>
        <Button size="sm" variant="outline" onClick={() => setDialog("labels")}><Tag /> Labels…</Button>
        <Button size="sm" variant="outline" onClick={() => setDialog("taints")}><FileText /> Taints…</Button>
        {managed && (
          <Button size="sm" variant={showLogs ? "secondary" : "outline"} onClick={() => setShowLogs((v) => !v)}><ScrollText /> System logs</Button>
        )}
        {managed && member && (
          <Button size="sm" variant="outline" className="ml-auto text-red-400 hover:text-red-300" onClick={() => setDialog("remove")}>
            <Trash2 /> Remove from cluster…
          </Button>
        )}
      </div>
      {showLogs && <NodeLogs clusterId={clusterId} name={node.name} />}
      {dialog === "labels" && <LabelsDialog clusterId={clusterId} node={node} open onClose={() => setDialog(null)} />}
      {dialog === "taints" && <TaintsDialog clusterId={clusterId} node={node} open onClose={() => setDialog(null)} />}
      {dialog === "drain" && <DrainDialog clusterId={clusterId} node={node} open onClose={() => setDialog(null)} />}
      {dialog === "remove" && member && <RemoveNodeDialog clusterId={clusterId} member={member} open onClose={() => setDialog(null)} />}
    </div>
  );
}

// ---- add nodes ------------------------------------------------------------------------------

const SIZES = [
  { label: "2 CPU · 2 GB", cpu: 2, memory: 2048, disk: 20 },
  { label: "2 CPU · 4 GB", cpu: 2, memory: 4096, disk: 40 },
  { label: "4 CPU · 8 GB", cpu: 4, memory: 8192, disk: 80 },
];

export function AddNodesDialog({ cluster, open, onClose }: { cluster: ClusterDetail; open: boolean; onClose: () => void }) {
  const add = useAddNodes(cluster.id);
  const notify = useOperationToast();
  const machines = useMachines();
  const connections = useConnections();
  const credentials = useCredentials();
  const cpPool = cluster.spec.control_plane.name ?? "control-plane";
  const pools = [
    ...(cluster.spec.control_plane.endpoint ? [cpPool] : []),
    ...cluster.spec.worker_pools.map((p) => p.name),
  ];
  const [pool, setPool] = useState(cluster.spec.worker_pools[0]?.name ?? "__new");
  const [newPool, setNewPool] = useState("workers");
  const [selected, setSelected] = useState<string[]>([]);
  const [count, setCount] = useState("0");
  const [size, setSize] = useState(SIZES[0]);
  const [connectionId, setConnectionId] = useState(cluster.spec.provider_connection_id ?? "");
  const [credentialId, setCredentialId] = useState(cluster.spec.machine_credential_id ?? "");
  const [error, setError] = useState<string | null>(null);

  const isCp = pool === cpPool;
  const available = (machines.data ?? []).filter((m) => !m.cluster_name && m.lifecycle_state === "ready");
  const provisioners = (connections.data ?? []).filter((c) => c.capabilities.includes("provision"));
  const keys = (credentials.data?.items ?? []).filter((c) => c.kind === "ssh_private_key");
  const n = Number(count) || 0;
  const poolName = pool === "__new" ? newPool : pool;
  const problems = [
    pool === "__new" && (!DNS_LABEL.test(newPool) || newPool === cpPool || pools.includes(newPool)) && "Choose a new, valid pool name",
    !selected.length && n < 1 && "Select machines or a number to provision",
    n > 0 && (!connectionId || !credentialId) && "Choose a connection and SSH key for new machines",
    isCp && n > 0 && size.cpu < 2 && "Control planes need at least 2 CPUs",
  ].filter(Boolean) as string[];

  const submit = () => {
    setError(null);
    add.mutate(
      {
        pool: poolName,
        existing_machine_ids: selected,
        provision_count: n,
        machine_template: n > 0 ? { cpu_cores: size.cpu, memory_mb: size.memory, disk_gb: size.disk } : null,
        provider_connection_id: n > 0 ? connectionId : null,
        machine_credential_id: n > 0 ? credentialId : null,
      },
      { onSuccess: (op) => { notify(`Adding nodes to ${cluster.name}`, op.id); onClose(); }, onError: (e) => setError(errorText(e)) },
    );
  };

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>Add nodes to {cluster.name}</DialogTitle>
          <DialogDescription>
            Machines are prepared, given Kubernetes {cluster.kubernetes_version} and joined. The cluster keeps running.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-5">
          <div className="grid gap-3 md:grid-cols-2">
            <div className="space-y-1.5">
              <Label>Node pool</Label>
              <Select value={pool} onValueChange={setPool}>
                <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
                <SelectContent>
                  {pools.map((p) => <SelectItem key={p} value={p}>{p === cpPool ? `${p} (control plane)` : p}</SelectItem>)}
                  <SelectItem value="__new">New worker pool…</SelectItem>
                </SelectContent>
              </Select>
              {!cluster.spec.control_plane.endpoint && (
                <p className="text-xs text-muted-foreground">
                  Control planes cannot be added: this cluster has no shared API endpoint (kube-vip or load balancer).
                </p>
              )}
            </div>
            {pool === "__new" && (
              <div className="space-y-1.5">
                <Label htmlFor="new-pool">Pool name</Label>
                <Input id="new-pool" value={newPool} onChange={(e) => setNewPool(e.target.value)} />
              </div>
            )}
          </div>

          <div className="space-y-2">
            <p className="text-sm font-medium">Registered machines</p>
            {available.length === 0 ? (
              <p className="text-sm text-muted-foreground">No free, ready machines in the inventory.</p>
            ) : (
              <ul className="divide-y divide-border/60 rounded-md border border-border">
                {available.map((m) => (
                  <li key={m.id}>
                    <label className={cn("flex items-center gap-3 px-3 py-2 text-sm", "cursor-pointer hover:bg-muted/30")}>
                      <input
                        type="checkbox"
                        checked={selected.includes(m.id)}
                        onChange={(e) => setSelected(e.target.checked ? [...selected, m.id] : selected.filter((id) => id !== m.id))}
                      />
                      <span className="font-medium">{m.name}</span>
                      <span className="text-xs text-muted-foreground">{m.os_info.pretty_name}</span>
                    </label>
                  </li>
                ))}
              </ul>
            )}
          </div>

          <div className="space-y-3 rounded-md border border-dashed border-border p-3">
            <div className="flex flex-wrap items-end gap-3">
              <div className="space-y-1.5">
                <Label htmlFor="provision-count">New machines</Label>
                <Input id="provision-count" value={count} onChange={(e) => setCount(e.target.value.replace(/\D/g, "").slice(0, 2))} className="w-20" inputMode="numeric" />
              </div>
              {n > 0 && (
                <div className="flex flex-wrap gap-2">
                  {SIZES.map((s) => (
                    <Button key={s.label} type="button" size="sm" variant={size === s ? "secondary" : "outline"} onClick={() => setSize(s)}>{s.label}</Button>
                  ))}
                </div>
              )}
            </div>
            {n > 0 &&
              (provisioners.length === 0 ? (
                <p className="text-sm text-muted-foreground">
                  No connection can provision machines. <Link href="/infrastructure/providers" className="underline underline-offset-4">Add one</Link>.
                </p>
              ) : (
                <div className="grid gap-3 md:grid-cols-2">
                  <Select value={connectionId} onValueChange={setConnectionId}>
                    <SelectTrigger className="w-full"><SelectValue placeholder="Connection" /></SelectTrigger>
                    <SelectContent>{provisioners.map((c) => <SelectItem key={c.id} value={c.id}>{c.name} · {c.provider_name}</SelectItem>)}</SelectContent>
                  </Select>
                  <Select value={credentialId} onValueChange={setCredentialId}>
                    <SelectTrigger className="w-full"><SelectValue placeholder="SSH key" /></SelectTrigger>
                    <SelectContent>{keys.map((c) => <SelectItem key={c.id} value={c.id}>{c.name}</SelectItem>)}</SelectContent>
                  </Select>
                </div>
              ))}
          </div>
          {problems.length > 0 && <ul className="space-y-1 text-sm text-amber-300/90">{problems.map((p) => <li key={p}>• {p}</li>)}</ul>}
          {error && <p role="alert" className="rounded border border-red-500/30 bg-red-500/5 p-2 text-sm text-red-300">{error}</p>}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>Cancel</Button>
          <Button onClick={submit} disabled={problems.length > 0 || add.isPending}>
            <Rocket /> {add.isPending ? "Starting…" : `Add ${selected.length + n} node${selected.length + n === 1 ? "" : "s"}`}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
