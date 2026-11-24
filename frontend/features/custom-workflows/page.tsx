"use client";
import Link from "next/link";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { ConfirmDialog } from "@/components/common/confirm-dialog";
import { PageHeader, ErrorState, EmptyState } from "@/components/common/page";
import { SearchableChoice } from "@/components/common/searchable-choice";
import { YamlEditor } from "@/components/common/yaml-editor";
import { useCan } from "@/components/providers/auth";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ClusterScope } from "@/features/kubernetes/shared";
import { api } from "@/lib/api/client";

type Workflow = { id: string; name: string; generation: number; step_count: number };
type Review = { id: string; name: string; expires_at: string; steps: { position: number; action: string; kind: string; namespace: string; name: string; replicas?: number; timeout_seconds: number }[] };
type History = { id: string; created_at: string; operation_id?: string | null };
const TEMPLATE = `steps:
  - action: cluster.check
# Other supported actions:
# - action: resource.scale
#   kind: Deployment
#   namespace: default
#   name: my-app
#   replicas: 2
# - action: resource.restart
#   kind: Deployment
#   namespace: default
#   name: my-app
# - action: resource.wait
#   kind: Deployment
#   namespace: default
#   name: my-app
#   timeout_seconds: 120
`;

function Editor({ base, row, initial, close, done }: { base: string; row?: Workflow; initial: string; close: () => void; done: (row: Workflow) => void }) {
  const [name, setName] = useState(row?.name ?? ""); const [text, setText] = useState(initial); const [busy, setBusy] = useState(false); const [error, setError] = useState<unknown>(null);
  const epoch = useRef(0); useEffect(() => () => { epoch.current += 1; }, []);
  const save = async () => { const request = epoch.current; setBusy(true); setError(null); try { const data = { name, document: text, generation: row?.generation }; const value = row ? await api.put<Workflow>(base + "/" + row.id, data) : await api.post<Workflow>(base, data); if (request === epoch.current) done(value); } catch (e) { if (request === epoch.current) setError(e); } finally { if (request === epoch.current) setBusy(false); } };
  return <section className="space-y-3 rounded border p-4"><h2 className="font-semibold">{row ? "Edit workflow" : "Create workflow"}</h2><Input aria-label="Workflow name" value={name} disabled={busy} onChange={(e) => setName(e.target.value)} placeholder="Workflow name" /><div aria-label="Workflow YAML editor"><YamlEditor value={text} readOnly={busy} onChange={setText} height="360px" /></div><p className="text-sm text-muted-foreground">Up to 20 ordered steps. Scale supports Deployments and StatefulSets; restart supports Deployments; readiness waits support Pods, Jobs, Deployments, StatefulSets and DaemonSets. Changing a workflow revokes pending reviews and stops further actions in active runs.</p><div className="flex gap-2"><Button disabled={busy || !name || text.length > 131072} onClick={() => void save()}>Save workflow</Button><Button variant="ghost" disabled={busy} onClick={close}>Close workflow editor</Button></div>{error !== null && <ErrorState error={error} />}</section>;
}

function WorkflowDetail({ base, row, changed }: { base: string; row: Workflow; changed: (row?: Workflow) => void }) {
  const manage = useCan("organization.manage"); const write = useCan("clusters.write"); const canReveal = useCan("secrets.reveal");
  const [raw, setRaw] = useState<string | null>(null); const [editing, setEditing] = useState(false); const [review, setReview] = useState<Review | null>(null); const [operation, setOperation] = useState<string | null>(null); const [confirm, setConfirm] = useState<"run" | "forget" | null>(null); const [busy, setBusy] = useState(false); const [error, setError] = useState<unknown>(null);
  const epoch = useRef(0); useEffect(() => () => { epoch.current += 1; }, []);
  const history = useQuery({ queryKey: ["custom-workflow-history", row.id], queryFn: () => api.get<History[]>(base + "/" + row.id + "/history"), refetchInterval: 5000 });
  const act = async (action: "reveal" | "review" | "run" | "forget") => { const request = epoch.current; setBusy(true); setError(null); try {
    const path = base + "/" + row.id + "/" + action;
    if (action === "reveal") { const value = await api.post<{ yaml: string }>(path); if (request === epoch.current) setRaw(value.yaml); }
    if (action === "review") { setReview(null); const value = await api.post<Review>(path); if (request === epoch.current) setReview(value); }
    if (action === "run") { const value = await api.post<{ id: string }>(path, { review_id: review?.id, confirm: row.name }); if (request === epoch.current) { setOperation(value.id); setReview(null); setConfirm(null); void history.refetch(); } }
    if (action === "forget") { await api.post(path, { confirm: row.name }); if (request === epoch.current) changed(); }
  } catch (e) { if (request === epoch.current) setError(e); } finally { if (request === epoch.current) setBusy(false); } };
  return <section className="space-y-3 rounded border p-4"><h2 className="font-semibold">Workflow: {row.name}</h2><p className="text-sm">Generation {row.generation} · {row.step_count} steps. Each run requires a fresh target review and confirmation.</p>
    <div className="flex flex-wrap gap-2">{canReveal && !raw && <Button variant="outline" disabled={busy} onClick={() => void act("reveal")}>Reveal workflow YAML</Button>}{canReveal && raw && <Button variant="outline" onClick={() => { setRaw(null); setEditing(false); }}>Hide workflow YAML</Button>}{manage && canReveal && raw && <Button variant="outline" onClick={() => setEditing(true)}>Edit workflow</Button>}{write && <Button disabled={busy} onClick={() => void act("review")}>Review workflow run</Button>}{manage && <Button variant="outline" disabled={busy} onClick={() => setConfirm("forget")}>Delete workflow</Button>}</div>
    {raw && canReveal && !editing && <YamlEditor value={raw} readOnly height="300px" />}{editing && manage && canReveal && raw && <Editor base={base} row={row} initial={raw} close={() => setEditing(false)} done={changed} />}
    {review && <div className="space-y-2 rounded border p-3"><h3 className="font-medium">Reviewed workflow run</h3><p className="text-xs">Expires {new Date(review.expires_at).toLocaleTimeString()}. Targets are bound by UID and mutation content; controller status remains separate.</p><ol className="list-decimal space-y-1 pl-5 text-sm">{review.steps.map((step) => <li key={step.position}>{step.action}{step.action !== "cluster.check" ? ` · ${step.kind} ${step.namespace}/${step.name}` : ""}{step.action === "resource.scale" ? ` → ${step.replicas} replicas` : ""}</li>)}</ol><Button disabled={busy} onClick={() => setConfirm("run")}>Confirm workflow run</Button></div>}
    {operation && <p role="status" className="text-sm">Workflow queued. <Link className="underline" href={`/operations/${operation}`}>View run progress</Link></p>}{error !== null && <ErrorState error={error} />}
    <h3 className="font-medium">Run history</h3>{history.isError && <ErrorState error={history.error} />}{history.data?.map((item) => <p key={item.id} className="text-sm">{new Date(item.created_at).toLocaleString()} · {item.operation_id ? <Link className="underline" href={`/operations/${item.operation_id}`}>View workflow operation</Link> : "Reviewed; not submitted"}</p>)}
    <ConfirmDialog key={`${confirm}:${review?.id}`} open={confirm !== null} onOpenChange={(open) => { if (!open) setConfirm(null); }} title={confirm === "forget" ? "Delete workflow" : "Run reviewed workflow"} description={confirm === "forget" ? "Remove this reusable definition and revoke further actions in active runs. Changes already accepted by Kubernetes remain." : "Execute these ordered Kubernetes actions. Runs expose progress, cancellation and retry through Operations. Completed actions remain if a later step fails or is cancelled; there is no automatic rollback."} confirmText={row.name} confirmLabel={confirm === "forget" ? "Delete workflow definition" : "Execute workflow"} pending={busy} onConfirm={() => void act(confirm === "forget" ? "forget" : "run")} />
  </section>;
}

function ClusterWorkflows({ id }: { id: string }) {
  const client = useQueryClient(); const manage = useCan("organization.manage"); const canReveal = useCan("secrets.reveal"); const write = useCan("clusters.write"); const [selected, setSelected] = useState(""); const [creating, setCreating] = useState(false); const base = `/clusters/${id}/custom-workflows`;
  const list = useQuery({ queryKey: ["custom-workflows", id], queryFn: () => api.get<Workflow[]>(base) }); const row = list.data?.find((r) => r.id === selected);
  const changed = (value?: Workflow) => { setCreating(false); setSelected(value?.id ?? ""); void client.invalidateQueries({ queryKey: ["custom-workflows", id] }); };
  return <div className="space-y-4"><div className="flex flex-wrap items-end gap-3"><SearchableChoice label="Custom workflow" value={selected} options={list.data?.map((r) => ({ value: r.id, label: r.name })) ?? []} change={(value) => { setSelected(value); setCreating(false); }} /><Button variant="outline" onClick={() => list.refetch()}>Refresh workflows</Button>{manage && <Button onClick={() => { setCreating(true); setSelected(""); }}>Create workflow</Button>}</div>{list.isPending && <p>Loading workflows…</p>}{list.isError && <ErrorState error={list.error} />}{list.data?.length === 0 && !creating && <EmptyState title="No custom workflows" description="Administrators can create reusable Kubernetes action sequences." />}{creating && manage && <Editor base={base} initial={TEMPLATE} close={() => setCreating(false)} done={changed} />}{row && <WorkflowDetail key={`${row.id}:${row.generation}:${manage}:${canReveal}:${write}`} base={base} row={row} changed={changed} />}</div>;
}

export function CustomWorkflowsPage() { return <div className="space-y-5"><PageHeader eyebrow="Operations" title="Custom workflows" description="Review and run reusable Kubernetes action sequences with progress, cancellation and checkpoints." /><ClusterScope>{(id) => <ClusterWorkflows key={id} id={id} />}</ClusterScope></div>; }
