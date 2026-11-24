"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { ConfirmDialog } from "@/components/common/confirm-dialog";
import { ErrorState, PageHeader } from "@/components/common/page";
import { useCan } from "@/components/providers/auth";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ClusterScope } from "@/features/kubernetes/shared";
import { api } from "@/lib/api/client";

type Review = { id: string; name: string; expires_at: string; command: string; read_only: boolean };
type History = { id: string; created_at: string; command: string; operation_id: string | null };
type Result = { output: string; truncated: boolean; observed_at: string; row_count: number };
type Output = { id: string; operation_id: string | null; result: Result | null };
const TERMINAL = new Set(["succeeded", "failed", "cancelled"]);
const EXAMPLES = ["kubectl get nodes", "kubectl get pods -A", "kubectl get deployments -n default", "helm list -A"];

function ClusterCommands({ id }: { id: string }) {
  const base = `/clusters/${id}/commands`;
  const [command, setCommand] = useState(EXAMPLES[0]);
  const [review, setReview] = useState<Review | null>(null);
  const [confirm, setConfirm] = useState(false);
  const [operation, setOperation] = useState<string | null>(null);
  const [output, setOutput] = useState<Output | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const epoch = useRef(0);
  useEffect(() => () => { epoch.current += 1; }, []);
  const history = useQuery({
    queryKey: ["command-center-history", id],
    queryFn: () => api.get<History[]>(base + "/history"),
    refetchInterval: 5000,
  });
  const status = useQuery({
    queryKey: ["command-center-operation", id, operation],
    queryFn: () => api.get<{ status: string }>(`/operations/${operation}`),
    enabled: !!operation,
    refetchInterval: (query) => TERMINAL.has(query.state.data?.status ?? "") ? false : 1500,
  });
  const edit = (value: string) => { setCommand(value); setReview(null); setConfirm(false); setOutput(null); };
  const act = async (action: "review" | "run" | "output", row?: History) => {
    const request = epoch.current;
    setBusy(true); setError(null);
    try {
      if (action === "review") {
        setReview(null); setOutput(null);
        const value = await api.post<Review>(base + "/reviews", { command });
        if (request === epoch.current) setReview(value);
      } else if (action === "run" && review) {
        const value = await api.post<{ id: string }>(base + "/run", { review_id: review.id, confirm: review.name });
        if (request === epoch.current) { setOperation(value.id); setReview(null); setConfirm(false); void history.refetch(); }
      } else if (row) {
        const value = await api.get<Output>(`${base}/${row.id}/output`);
        if (request === epoch.current) { setOutput(value); setOperation(row.operation_id); }
      }
    } catch (e) { if (request === epoch.current) setError(e); }
    finally { if (request === epoch.current) setBusy(false); }
  };
  return <div className="space-y-5">
    <section className="space-y-3 rounded border p-4">
      <h2 className="font-semibold">Read a cluster summary</h2>
      <p className="text-sm text-muted-foreground">Supported: kubectl get RESOURCE [NAME] [-n NAMESPACE | -A], helm list [-n NAMESPACE | -A], and helm history RELEASE [-n NAMESPACE]. Namespace defaults to default. Results contain selected summary fields.</p>
      <details className="text-sm"><summary className="cursor-pointer">Supported Kubernetes resources and limits</summary><p className="mt-2">Nodes, namespaces, pods, services, deployments, ReplicaSets, StatefulSets, DaemonSets, jobs, CronJobs, persistent volume claims, persistent volumes and storage classes. Lists return at most 100 rows; Helm history returns at most 50 revisions. Output is capped at 64 KiB. Secrets, full YAML/JSON, logs, resource changes and shell syntax are unavailable here.</p></details>
      <div className="flex flex-wrap gap-2">{EXAMPLES.map((example) => <Button key={example} size="sm" variant="outline" disabled={busy} onClick={() => edit(example)}>{example}</Button>)}</div>
      <label htmlFor="cluster-command" className="text-sm font-medium">Command</label>
      <Input id="cluster-command" className="font-mono" value={command} maxLength={512} disabled={busy} onChange={(event) => edit(event.target.value)} autoComplete="off" spellCheck={false} />
      <Button disabled={busy || !command.trim()} onClick={() => void act("review")}>Review command</Button>
      {review && <div className="space-y-2 rounded border p-3">
        <h3 className="font-medium">Reviewed command</h3><pre className="overflow-auto text-sm">{review.command}</pre>
        <p className="text-sm">Target: {review.name}. Review expires {new Date(review.expires_at).toLocaleTimeString()}. Execution reads the cluster’s current state.</p>
        <Button disabled={busy} onClick={() => setConfirm(true)}>Confirm and run</Button>
      </div>}
    </section>
    {operation && <div role="status" className="space-y-2 text-sm"><p>Command operation: {status.data?.status ?? "loading"}. <Link className="underline" href={`/operations/${operation}`}>View progress or cancel</Link></p><p>Open the matching history entry to retrieve output after completion.</p>{status.isError && <ErrorState error={status.error} />}</div>}
    {error !== null && <ErrorState error={error} />}
    {output && <section aria-label="Command output" className="space-y-2 rounded border p-4"><div className="flex items-center justify-between"><h2 className="font-semibold">Command output</h2><Button variant="ghost" onClick={() => setOutput(null)}>Hide output</Button></div>{output.result ? <><p className="text-sm">{output.result.row_count} rows · Observed {new Date(output.result.observed_at).toLocaleString()}</p>{output.result.truncated && <p role="alert" className="text-sm text-amber-600">Results were truncated. Narrow the namespace or resource name.</p>}<pre className="max-h-96 overflow-auto whitespace-pre-wrap break-all rounded bg-muted p-3 text-xs">{output.result.output}</pre></> : <p className="text-sm">No output is available yet. Check operation progress, then open this history entry again.</p>}</section>}
    <section className="space-y-2"><h2 className="font-semibold">Your recent commands</h2>{history.isError && <ErrorState error={history.error} />}{history.isPending && <p>Loading command history…</p>}{history.data?.length === 0 && <p className="text-sm text-muted-foreground">No command reviews for this cluster yet.</p>}{history.data?.map((row) => <div key={row.id} className="flex flex-wrap items-center gap-3 rounded border p-3 text-sm"><code className="break-all">{row.command}</code><span className="text-muted-foreground">{new Date(row.created_at).toLocaleString()}</span>{row.operation_id ? <><Link className="underline" href={`/operations/${row.operation_id}`}>Operation</Link><Button variant="outline" size="sm" disabled={busy} onClick={() => void act("output", row)}>View output</Button></> : <span>Reviewed; not submitted</span>}</div>)}</section>
    <ConfirmDialog key={review?.id} open={confirm && !!review} onOpenChange={setConfirm} title="Run reviewed command" description="Read the selected cluster using its platform connection. Only this reviewed command will run. Output is private to your account; operation history contains execution metadata. Type the cluster name to confirm." confirmText={review?.name} confirmLabel="Run command" pending={busy} onConfirm={() => void act("run")} />
  </div>;
}

export function CommandCenterPage() {
  const canExecute = useCan("terminal.exec");
  const canWrite = useCan("clusters.write");
  const allowed = canExecute && canWrite;
  return <div className="space-y-5"><PageHeader eyebrow="Operations" title="Command center" description="Review and run bounded Kubernetes and Helm summary commands for the selected cluster." />{allowed ? <ClusterScope>{(id) => <ClusterCommands key={id} id={id} />}</ClusterScope> : <p role="alert">Command Center requires operator or administrator access.</p>}</div>;
}
