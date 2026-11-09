"use client";

import { Eye, Pause, Play, RotateCcw, RotateCw, Scaling, Trash2, Zap } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import { ConfirmDialog } from "@/components/common/confirm-dialog";
import { StatusPill } from "@/components/common/status";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { LiveError, PodStatusBadge } from "@/features/kubernetes/shared";
import {
  useCronJobSuspend,
  useCronJobTrigger,
  useDeleteScalingPolicy,
  useDeleteResource,
  useResource,
  useRestart,
  useRevealSecret,
  useRollback,
  useScale,
  useSetScalingPolicy,
  type ResourceDetail,
  type ResourceKind,
} from "@/lib/api/workloads";
import { formatAge, formatDateTime } from "@/lib/format";
import { KIND_CONFIG, RolloutBadge } from "./kinds";
import { PodLogs, PodTerminal } from "./pod-io";
import { YamlApplyPanel } from "./yaml-apply";

function Events({ detail }: { detail: ResourceDetail }) {
  if (!detail.events.length) return <p className="text-sm text-muted-foreground">No recent events for this object.</p>;
  return (
    <ul className="space-y-2 text-sm">
      {detail.events.map((e, i) => (
        <li key={i} className="flex gap-2">
          <StatusPill tone={e.type === "Warning" ? "warning" : "neutral"}>{e.reason}</StatusPill>
          <span className="flex-1 text-muted-foreground">{e.message}</span>
          <span className="shrink-0 text-xs text-muted-foreground">{formatAge(e.last_seen)} ×{e.count}</span>
        </li>
      ))}
    </ul>
  );
}

function PodsTable({ detail }: { detail: ResourceDetail }) {
  const pods = detail.pods ?? [];
  if (!pods.length) return <p className="text-sm text-muted-foreground">No pods.</p>;
  return (
    <Table>
      <TableHeader><TableRow><TableHead>Pod</TableHead><TableHead>Status</TableHead><TableHead>Ready</TableHead><TableHead>Restarts</TableHead><TableHead>Node</TableHead></TableRow></TableHeader>
      <TableBody>
        {pods.map((p) => (
          <TableRow key={p.name}>
            <TableCell className="font-mono text-xs">{p.name}</TableCell>
            <TableCell><PodStatusBadge status={p.status} problem={p.problem} phase={p.phase} /></TableCell>
            <TableCell>{p.ready}</TableCell>
            <TableCell>{p.restarts}</TableCell>
            <TableCell className="text-sm">{p.node ?? "—"}</TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

function History({ clusterId, detail }: { clusterId: string; detail: ResourceDetail }) {
  const rollback = useRollback(clusterId);
  const [target, setTarget] = useState<number | null>(null);
  const { namespace, name } = detail.summary;
  return (
    <>
      <Table>
        <TableHeader><TableRow><TableHead>Revision</TableHead><TableHead>Images</TableHead><TableHead>Change cause</TableHead><TableHead>Created</TableHead><TableHead /></TableRow></TableHeader>
        <TableBody>
          {(detail.history ?? []).map((h) => (
            <TableRow key={h.replicaset}>
              <TableCell>{h.revision} {h.current && <StatusPill tone="success">current</StatusPill>}</TableCell>
              <TableCell className="font-mono text-xs">{h.images.join(", ")}</TableCell>
              <TableCell className="text-xs text-muted-foreground">{h.change_cause ?? "—"}</TableCell>
              <TableCell className="text-sm text-muted-foreground">{formatDateTime(h.created)}</TableCell>
              <TableCell className="text-right">
                {!h.current && h.revision && (
                  <Button size="xs" variant="outline" onClick={() => setTarget(h.revision)}><RotateCcw /> Roll back</Button>
                )}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
      <ConfirmDialog
        open={target !== null}
        onOpenChange={(open) => !open && setTarget(null)}
        destructive={false}
        title={`Roll back ${name} to revision ${target}?`}
        description={<p>The pod template of revision {target} becomes current and a new rollout starts.</p>}
        confirmLabel="Roll back"
        pending={rollback.isPending}
        onConfirm={() =>
          target &&
          rollback.mutate(
            { namespace, name, revision: target },
            { onSuccess: () => { toast.success(`Rolling back to revision ${target}`); setTarget(null); }, onError: (e) => toast.error("Rollback failed", { description: e.message }) },
          )
        }
      />
    </>
  );
}

function DataView({ clusterId, detail }: { clusterId: string; detail: ResourceDetail }) {
  const reveal = useRevealSecret(clusterId);
  const isSecret = detail.kind === "Secret";
  const data = (detail.object.data ?? {}) as Record<string, string>;
  const revealed = reveal.data?.values;
  return (
    <div className="space-y-3">
      {isSecret && !revealed && (
        <Button size="sm" variant="outline" disabled={reveal.isPending} onClick={() => reveal.mutate({ namespace: detail.summary.namespace, name: detail.summary.name })}>
          <Eye /> Reveal values (audited)
        </Button>
      )}
      <dl className="space-y-2">
        {Object.keys(data).sort().map((key) => (
          <div key={key} className="rounded-md border border-border p-2">
            <dt className="font-mono text-xs text-muted-foreground">{key}</dt>
            <dd className="font-mono text-xs whitespace-pre-wrap break-all">
              {isSecret ? (revealed ? (revealed[key]?.binary ? `(binary, base64) ${revealed[key].text}` : revealed[key]?.text) : "••••••••") : data[key]}
            </dd>
          </div>
        ))}
      </dl>
      {!Object.keys(data).length && <p className="text-sm text-muted-foreground">No data.</p>}
    </div>
  );
}

function Overview({ detail }: { detail: ResourceDetail }) {
  const skip = new Set(["name", "namespace", "labels", "rollout", "problem"]);
  const entries = Object.entries(detail.summary).filter(([k, v]) => !skip.has(k) && v !== null && v !== undefined && !(typeof v === "object" && !Array.isArray(v)));
  return (
    <div className="space-y-4 text-sm">
      {detail.rollout && (
        <p className="flex items-center gap-2"><RolloutBadge rollout={detail.rollout} /> {detail.rollout.message}</p>
      )}
      <dl className="grid grid-cols-[10rem_1fr] gap-x-3 gap-y-1">
        {entries.map(([k, v]) => (
          <div key={k} className="contents">
            <dt className="text-muted-foreground">{k.replaceAll("_", " ")}</dt>
            <dd className="font-mono text-xs leading-5">{k === "created" ? formatDateTime(String(v)) : Array.isArray(v) ? v.join(", ") || "—" : String(v)}</dd>
          </div>
        ))}
      </dl>
      <div className="flex flex-wrap gap-1">
        {Object.entries(detail.summary.labels as Record<string, string>).map(([k, v]) => <span key={k} className="rounded bg-muted px-1.5 font-mono text-[11px]">{k}={v}</span>)}
      </div>
    </div>
  );
}

function ScaleDialog({ open, onOpenChange, clusterId, kind, detail }: { open: boolean; onOpenChange: (o: boolean) => void; clusterId: string; kind: ResourceKind; detail: ResourceDetail }) {
  const scale = useScale(clusterId, kind);
  const current = Number((detail.summary.replicas ?? detail.summary.desired ?? 1) as number);
  const [value, setValue] = useState(String(current));
  const n = Number(value);
  const valid = Number.isInteger(n) && n >= 0 && n <= 1000;
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-sm">
        <DialogHeader>
          <DialogTitle>Scale {detail.summary.name}</DialogTitle>
          <DialogDescription>Currently {current} replica(s).</DialogDescription>
        </DialogHeader>
        <Input type="number" min={0} max={1000} value={value} onChange={(e) => setValue(e.target.value)} aria-label="Replicas" />
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button
            disabled={!valid || n === current || scale.isPending}
            onClick={() => scale.mutate({ namespace: detail.summary.namespace, name: detail.summary.name, replicas: n }, {
              onSuccess: () => { toast.success(`Scaled to ${n}`); onOpenChange(false); },
              onError: (e) => toast.error("Scale failed", { description: e.message }),
            })}
          >
            Scale to {valid ? n : "…"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function ScalingPolicyDialog({ open, onOpenChange, clusterId, detail }: { open: boolean; onOpenChange: (o: boolean) => void; clusterId: string; detail: ResourceDetail }) {
  const policy = detail.scaling_policy as { spec?: { minReplicas?: number; maxReplicas?: number; metrics?: { resource?: { name?: string; target?: { averageUtilization?: number } } }[]; behavior?: { scaleDown?: { stabilizationWindowSeconds?: number } } } } | null | undefined;
  const [minimum, setMinimum] = useState(String(policy?.spec?.minReplicas ?? 1));
  const [maximum, setMaximum] = useState(String(policy?.spec?.maxReplicas ?? 5));
  const [target, setTarget] = useState(String(policy?.spec?.metrics?.[0]?.resource?.target?.averageUtilization ?? 70));
  const [metric, setMetric] = useState<"cpu" | "memory">(policy?.spec?.metrics?.[0]?.resource?.name === "memory" ? "memory" : "cpu");
  const [stabilization, setStabilization] = useState(String(policy?.spec?.behavior?.scaleDown?.stabilizationWindowSeconds ?? 300));
  const set = useSetScalingPolicy(clusterId);
  const remove = useDeleteScalingPolicy(clusterId);
  const nMin = Number(minimum), nMax = Number(maximum), nTarget = Number(target), nStabilization = Number(stabilization);
  const valid = Number.isInteger(nMin) && Number.isInteger(nMax) && Number.isInteger(nTarget) && Number.isInteger(nStabilization) && nMin >= 1 && nMax >= nMin && nMax <= 1000 && nTarget >= 1 && nTarget <= 100 && nStabilization >= 0 && nStabilization <= 3600;
  const identity = { namespace: detail.summary.namespace, name: detail.summary.name };
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader><DialogTitle>{policy ? "Update scaling policy" : "Add scaling policy"}</DialogTitle><DialogDescription>Automatically scale this Deployment from CPU or memory utilization. Resource requests must be set on its containers.</DialogDescription></DialogHeader>
        <div className="grid grid-cols-2 gap-3">
          <label className="space-y-1 text-sm">Minimum replicas<Input type="number" min={1} max={1000} value={minimum} onChange={(e) => setMinimum(e.target.value)} /></label>
          <label className="space-y-1 text-sm">Maximum replicas<Input type="number" min={1} max={1000} value={maximum} onChange={(e) => setMaximum(e.target.value)} /></label>
          <label className="space-y-1 text-sm">Metric<Select value={metric} onValueChange={(v) => setMetric(v as "cpu" | "memory")}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectItem value="cpu">CPU</SelectItem><SelectItem value="memory">Memory</SelectItem></SelectContent></Select></label>
          <label className="space-y-1 text-sm">Target utilization (%)<Input type="number" min={1} max={100} value={target} onChange={(e) => setTarget(e.target.value)} /></label>
          <label className="col-span-2 space-y-1 text-sm">Scale-down stabilization (seconds)<Input type="number" min={0} max={3600} value={stabilization} onChange={(e) => setStabilization(e.target.value)} /></label>
        </div>
        <DialogFooter className="sm:justify-between">
          {policy ? <Button variant="destructive" disabled={remove.isPending} onClick={() => remove.mutate(identity, { onSuccess: () => { toast.success("Scaling policy removed"); onOpenChange(false); }, onError: (e) => toast.error("Could not remove policy", { description: e.message }) })}>Remove policy</Button> : <span />}
          <div className="flex gap-2"><Button variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button><Button disabled={!valid || set.isPending} onClick={() => set.mutate({ ...identity, minimum: nMin, maximum: nMax, metric, target_utilization: nTarget, scale_down_stabilization_seconds: nStabilization }, { onSuccess: () => { toast.success("Scaling policy saved"); onOpenChange(false); }, onError: (e) => toast.error("Could not save policy", { description: e.message }) })}>Save policy</Button></div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function ResourceDetailDialog({
  clusterId,
  kind,
  target,
  onClose,
}: {
  clusterId: string;
  kind: ResourceKind;
  target: { namespace: string; name: string } | null;
  onClose: () => void;
}) {
  const config = KIND_CONFIG[kind];
  const detail = useResource(clusterId, kind, target?.namespace ?? null, target?.name ?? null);
  const restart = useRestart(clusterId, kind);
  const remove = useDeleteResource(clusterId, kind);
  const trigger = useCronJobTrigger(clusterId);
  const suspend = useCronJobSuspend(clusterId);
  const [scaling, setScaling] = useState(false);
  const [policyDialog, setPolicyDialog] = useState(false);
  const [restarting, setRestarting] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const d = detail.data;
  const isPod = kind === "pods";
  const containers = [...(d?.containers ?? []), ...(d?.init_containers ?? [])];

  return (
    <Dialog open={target !== null} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-h-[92vh] overflow-y-auto sm:max-w-5xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">{config.singular} <span className="font-mono">{target?.name}</span></DialogTitle>
          <DialogDescription>Namespace {target?.namespace} · live from the cluster</DialogDescription>
        </DialogHeader>
        {detail.isLoading && <Skeleton className="h-80" />}
        {detail.isError && <LiveError error={detail.error} clusterId={clusterId} onRetry={() => detail.refetch()} />}
        {d && target && (
          <>
            <div className="flex flex-wrap gap-2">
              {d.capabilities.scale && <Button size="sm" variant="outline" onClick={() => setScaling(true)}><Scaling /> Scale</Button>}
              {kind === "deployments" && <Button size="sm" variant="outline" onClick={() => setPolicyDialog(true)}><Scaling /> {d.scaling_policy ? "Scaling policy" : "Autoscale"}</Button>}
              {d.capabilities.restart && <Button size="sm" variant="outline" onClick={() => setRestarting(true)}><RotateCw /> Restart</Button>}
              {kind === "cronjobs" && (
                <>
                  <Button size="sm" variant="outline" disabled={trigger.isPending} onClick={() => trigger.mutate(target, { onSuccess: (r) => toast.success(`Started job ${r.job}`), onError: (e) => toast.error("Could not start job", { description: e.message }) })}><Zap /> Run now</Button>
                  <Button size="sm" variant="outline" disabled={suspend.isPending} onClick={() => suspend.mutate({ ...target, suspend: !d.summary.suspend }, { onError: (e) => toast.error("Failed", { description: e.message }) })}>
                    {d.summary.suspend ? <><Play /> Resume</> : <><Pause /> Suspend</>}
                  </Button>
                </>
              )}
              <Button size="sm" variant="outline" className="ml-auto" onClick={() => setDeleting(true)}><Trash2 className="text-red-400" /> Delete</Button>
            </div>
            <Tabs defaultValue={isPod ? "logs" : "overview"}>
              <TabsList className="flex-wrap">
                <TabsTrigger value="overview">Overview</TabsTrigger>
                {isPod && <TabsTrigger value="logs">Logs</TabsTrigger>}
                {isPod && <TabsTrigger value="terminal">Terminal</TabsTrigger>}
                {d.pods && <TabsTrigger value="pods">Pods ({d.pods.length})</TabsTrigger>}
                {d.history && <TabsTrigger value="history">History</TabsTrigger>}
                {d.jobs && <TabsTrigger value="jobs">Jobs ({d.jobs.length})</TabsTrigger>}
                {(kind === "configmaps" || kind === "secrets") && <TabsTrigger value="data">Data</TabsTrigger>}
                <TabsTrigger value="yaml">YAML</TabsTrigger>
                <TabsTrigger value="events">Events ({d.events.length})</TabsTrigger>
              </TabsList>
              <TabsContent value="overview" className="pt-4"><Overview detail={d} /></TabsContent>
              {isPod && <TabsContent value="logs" className="pt-4"><PodLogs clusterId={clusterId} namespace={target.namespace} pod={target.name} containers={containers} /></TabsContent>}
              {isPod && <TabsContent value="terminal" className="pt-4"><PodTerminal clusterId={clusterId} namespace={target.namespace} pod={target.name} containers={d.containers ?? []} /></TabsContent>}
              {d.pods && <TabsContent value="pods" className="pt-4"><PodsTable detail={d} /></TabsContent>}
              {d.history && <TabsContent value="history" className="pt-4"><History clusterId={clusterId} detail={d} /></TabsContent>}
              {d.jobs && (
                <TabsContent value="jobs" className="pt-4">
                  <ul className="space-y-1 text-sm">
                    {d.jobs.map((j) => <li key={j.name} className="flex gap-2"><span className="font-mono text-xs">{j.name}</span><StatusPill tone={j.status === "Complete" ? "success" : j.status === "Failed" ? "danger" : "info"}>{String(j.status)}</StatusPill><span className="text-muted-foreground">{formatAge(j.created)}</span></li>)}
                    {!d.jobs.length && <li className="text-muted-foreground">No jobs yet.</li>}
                  </ul>
                </TabsContent>
              )}
              {(kind === "configmaps" || kind === "secrets") && <TabsContent value="data" className="pt-4"><DataView clusterId={clusterId} detail={d} /></TabsContent>}
              <TabsContent value="yaml" className="pt-4">
                <YamlApplyPanel key={`${target.namespace}/${target.name}`} clusterId={clusterId} initial={d.yaml} defaultNamespace={target.namespace} allowNamespace={false} allowFiles={false} height="420px" />
              </TabsContent>
              <TabsContent value="events" className="pt-4"><Events detail={d} /></TabsContent>
            </Tabs>
            {d.capabilities.scale && scaling && <ScaleDialog open={scaling} onOpenChange={setScaling} clusterId={clusterId} kind={kind} detail={d} />}
            {kind === "deployments" && policyDialog && <ScalingPolicyDialog open={policyDialog} onOpenChange={setPolicyDialog} clusterId={clusterId} detail={d} />}
            <ConfirmDialog
              open={restarting}
              onOpenChange={setRestarting}
              destructive={false}
              title={`Restart ${target.name}?`}
              description={<p>Pods are replaced gradually according to the update strategy (like <span className="font-mono">kubectl rollout restart</span>).</p>}
              confirmLabel="Restart"
              pending={restart.isPending}
              onConfirm={() => restart.mutate(target, { onSuccess: () => { toast.success("Restart started"); setRestarting(false); }, onError: (e) => toast.error("Restart failed", { description: e.message }) })}
            />
            <ConfirmDialog
              open={deleting}
              onOpenChange={setDeleting}
              title={`Delete ${config.singular.toLowerCase()} ${target.name}?`}
              description={<p>The object is deleted from namespace {target.namespace}. {kind === "pods" ? "A controller may recreate it." : "Dependent objects are garbage-collected."}</p>}
              confirmText={target.name}
              confirmLabel="Delete"
              pending={remove.isPending}
              onConfirm={() => remove.mutate(target, { onSuccess: () => { toast.success(`Deleted ${target.name}`); setDeleting(false); onClose(); }, onError: (e) => toast.error("Delete failed", { description: e.message }) })}
            />
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}
