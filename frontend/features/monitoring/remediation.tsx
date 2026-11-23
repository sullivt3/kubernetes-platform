"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";

import { ErrorState } from "@/components/common/page";
import { ConfirmDialog } from "@/components/common/confirm-dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useCan } from "@/components/providers/auth";
import { api } from "@/lib/api/client";
import { useClusterNamespaces } from "@/lib/api/kube";
import { formatDateTime } from "@/lib/format";

type Policy = { enabled: boolean; namespaces: string[]; grace_seconds: number; cooldown_minutes: number };
type Proposal = {
  id: string; namespace: string; pod_name: string; deployment_name: string; reason: string; status: string;
  first_seen_at: string; last_seen_at: string; expires_at: string | null; approved_by: string | null;
  operation_id: string | null; operation_status: string | null; confirmation: string;
};
type Settings = { policy: Policy; observation: { status: string; checked_at: string } | null; requests: Proposal[] };

function PolicyForm({ id, policy, saved }: { id: string; policy: Policy; saved: () => void }) {
  const [draft, setDraft] = useState(policy);
  const namespaces = useClusterNamespaces(id);
  const choices = [...new Set([...(namespaces.data ?? []).map((n) => n.name), ...draft.namespaces])]
    .filter((n) => !n.startsWith("kube-") && n !== "kplatform-monitoring").sort();
  const save = useMutation({ mutationFn: () => api.put(`/clusters/${id}/monitoring/remediation`, draft), onSuccess: saved });
  return <div className="space-y-3 rounded border p-3">
    <label className="flex items-center gap-2"><input type="checkbox" checked={draft.enabled} onChange={(e) => setDraft({ ...draft, enabled: e.target.checked })} />Enable automatic detection</label>
    <fieldset><legend className="mb-2 text-sm font-medium">Eligible namespaces</legend>
      {namespaces.isError && <ErrorState error={namespaces.error} onRetry={() => namespaces.refetch()} />}
      <div className="grid max-h-40 gap-2 overflow-auto sm:grid-cols-2">{choices.map((name) => <label key={name} className="flex items-center gap-2 text-sm">
        <input type="checkbox" checked={draft.namespaces.includes(name)} onChange={(e) => setDraft({ ...draft, namespaces: e.target.checked ? [...draft.namespaces, name] : draft.namespaces.filter((n) => n !== name) })} />{name}
      </label>)}</div>
    </fieldset>
    <div className="grid gap-3 sm:grid-cols-2">
      <label className="space-y-1 text-sm">Failure observation window (seconds)<Input type="number" min={60} max={3600} value={draft.grace_seconds} onChange={(e) => setDraft({ ...draft, grace_seconds: Number(e.target.value) })} /></label>
      <label className="space-y-1 text-sm">Deployment cooldown (minutes)<Input type="number" min={5} max={1440} value={draft.cooldown_minutes} onChange={(e) => setDraft({ ...draft, cooldown_minutes: Number(e.target.value) })} /></label>
    </div>
    <p className="text-xs text-muted-foreground">Every replacement requires administrator approval. Saving policy changes revokes outstanding proposals and unexecuted approvals.</p>
    <Button disabled={save.isPending || (draft.enabled && !draft.namespaces.length)} onClick={() => save.mutate()}>Save remediation policy</Button>
    {save.isSuccess && <p role="status" className="text-sm">Remediation policy saved.</p>}
    {save.isError && <ErrorState error={save.error} />}
  </div>;
}

export function RemediationControls({ id }: { id: string }) {
  const admin = useCan("organization.manage");
  const client = useQueryClient();
  const [review, setReview] = useState<Proposal | null>(null);
  const query = useQuery({ queryKey: ["remediation", id], queryFn: () => api.get<Settings>(`/clusters/${id}/monitoring/remediation`), refetchInterval: 15000 });
  const refresh = () => { void client.invalidateQueries({ queryKey: ["remediation", id] }); };
  const scan = useMutation({ mutationFn: () => api.post(`/clusters/${id}/monitoring/remediation/scan`, {}), onSuccess: refresh });
  const approve = useMutation({ mutationFn: (p: Proposal) => api.post(`/clusters/${id}/monitoring/remediation/${p.id}/approve`, { confirm: p.confirmation }), onSuccess: () => { setReview(null); refresh(); } });
  const dismiss = useMutation({ mutationFn: (p: Proposal) => api.post(`/clusters/${id}/monitoring/remediation/${p.id}/dismiss`, {}), onSuccess: refresh });
  return <section className="space-y-3 rounded-lg border p-4">
    <h2 className="font-semibold">Automated remediation</h2>
    <p className="text-sm text-muted-foreground">Detect repeatedly failing stateless Deployment pods and propose one replacement. Administrators approve each attempt; the worker respects disruption budgets and verifies recovery.</p>
    {query.isPending && <p>Loading remediation settings…</p>}
    {query.isError && <ErrorState error={query.error} onRetry={() => query.refetch()} />}
    {query.data && <>
      <p className="text-sm">Detection {query.data.policy.enabled ? "enabled" : "disabled"} · scans every minute</p>
      {query.data.observation && <p className="text-xs text-muted-foreground">Last scan: {formatDateTime(query.data.observation.checked_at)} · {query.data.observation.status}</p>}
      {admin && <details><summary className="cursor-pointer text-sm font-medium">Configure remediation</summary><div className="mt-3">
        <PolicyForm key={JSON.stringify(query.data.policy)} id={id} policy={query.data.policy} saved={refresh} />
      </div></details>}
      {admin && <Button variant="outline" disabled={scan.isPending || !query.data.policy.enabled} onClick={() => scan.mutate()}>Check remediation now</Button>}
      {!query.data.requests.length && <p className="text-sm text-muted-foreground">No remediation observations yet.</p>}
      {query.data.requests.map((p) => <article key={p.id} className="space-y-2 rounded border p-3">
        <p className="font-mono text-sm">{p.namespace}/{p.pod_name}</p>
        <p className="text-sm">{p.reason} · {p.operation_status ?? p.status} · Deployment {p.deployment_name}</p>
        <p className="text-xs text-muted-foreground">First observed {formatDateTime(p.first_seen_at)} · last observed {formatDateTime(p.last_seen_at)}</p>
        {p.status === "pending" && p.expires_at && <p className="text-xs">Approval expires {formatDateTime(p.expires_at)}</p>}
        {p.approved_by && <p className="text-xs">Approved by {p.approved_by}</p>}
        {p.operation_id && <Link className="text-sm underline" href={`/operations/${p.operation_id}`}>View remediation operation</Link>}
        {admin && p.status === "pending" && <div className="flex gap-2">
          <Button onClick={() => { approve.reset(); setReview(p); }}>Review replacement</Button>
          <Button variant="outline" disabled={dismiss.isPending} onClick={() => dismiss.mutate(p)}>Dismiss</Button>
        </div>}
      </article>)}
    </>}
    {scan.isError && <ErrorState error={scan.error} />}
    {dismiss.isError && <ErrorState error={dismiss.error} />}
    <ConfirmDialog open={Boolean(review)} onOpenChange={(open) => { if (!open) setReview(null); }} title="Approve pod replacement?"
      description={<><p>Evict {review?.namespace}/{review?.pod_name} so its Deployment creates a replacement. Active requests may be interrupted. Disruption budgets are enforced; only stateless pods without local or persistent data volumes are eligible. This does not repair application configuration.</p>{approve.isError && <ErrorState error={approve.error} />}</>}
      confirmText={review?.confirmation} confirmLabel="Approve replacement" pending={approve.isPending} onConfirm={() => { if (review) approve.mutate(review); }} />
  </section>;
}
