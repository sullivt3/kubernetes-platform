"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useRef, useState } from "react";
import { ConfirmDialog } from "@/components/common/confirm-dialog";
import { EmptyState, ErrorState, PageHeader } from "@/components/common/page";
import { useCan } from "@/components/providers/auth";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Command, CommandInput, CommandList, CommandItem, CommandEmpty } from "@/components/ui/command";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { ClusterScope } from "@/features/kubernetes/shared";
import { Phase6Operation } from "@/features/kubernetes/phase6-operation";
import { api } from "@/lib/api/client";
import { useClusterNamespaces } from "@/lib/api/kube";

type Config = { url: string; branch: string; path: string; namespace: string; allow_private_network: boolean; ca_pem: string | null; ssh_host_key?: string | null };
type Resource = { kind: string; name: string; namespace: string; action?: string; reason?: string };
type Automation = { enabled: boolean; prune: boolean; allow_empty: boolean; interval_seconds: number; approved_by?: string; approved_at?: string };
type Review = { id: string; commit: string; expires_at: string; created_at?: string; resources: Resource[]; retained: Resource[]; prune?: Resource[]; prune_enabled?: boolean; automatic?: boolean; operation_id?: string; completed?: { status: string; pruned?: number } };
type Application = { id: string; name: string; configuration: Config; generation: number; authenticated: boolean; automation: Automation; next_reconcile_at: string | null; observation: { status?: string; commit?: string; checked_at?: string; synced_at?: string; last_synced_commit?: string; reconcile_operation_id?: string; error?: string }; inventory: Resource[] };

function Namespace({ value, names, change }: { value: string; names: string[]; change: (v: string) => void }) {
  const [open, setOpen] = useState(false);
  return <div className="space-y-1 text-sm"><span>Destination namespace</span><Popover open={open} onOpenChange={setOpen}><PopoverTrigger asChild><Button type="button" role="combobox" aria-label="Destination namespace" aria-expanded={open} variant="outline" className="w-full justify-between">{value || "Select namespace"}</Button></PopoverTrigger><PopoverContent className="p-0"><Command><CommandInput aria-label="Search destination namespace" placeholder="Search namespaces…" /><CommandList><CommandEmpty>No matching namespaces</CommandEmpty>{names.map((name) => <CommandItem key={name} value={name} onSelect={() => { change(name); setOpen(false); }}>{name}</CommandItem>)}</CommandList></Command></PopoverContent></Popover></div>;
}

function ApplicationForm({ base, current, close, changed, namespaces }: { base: string; current?: Application; close: () => void; changed: () => void; namespaces: string[] }) {
  const admin = useCan("organization.manage");
  const [name, setName] = useState(current?.name ?? "");
  const [config, setConfig] = useState<Config>(current?.configuration ?? { url: "", branch: "main", path: ".", namespace: namespaces.includes("default") ? "default" : "", allow_private_network: false, ca_pem: null });
  const [authenticated, setAuthenticated] = useState(current?.authenticated ?? false);
  const ssh = config.url.startsWith("ssh://") || /^[^@]+@[^:]+:/.test(config.url);
  const [mode, setMode] = useState(current && (current.configuration.url.startsWith("ssh://") || /^[^@]+@[^:]+:/.test(current.configuration.url)) ? "ssh" : "basic");
  const [passphrase, setPassphrase] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const mutation = useMutation({ mutationFn: () => {
    const body = { ...config, name, clear_credentials: Boolean(current?.authenticated && !authenticated), ...(authenticated && password ? { authentication: mode === "ssh" ? { mode, private_key: password, passphrase: passphrase || null } : { mode, username, password } } : {}) };
    return current ? api.put(base + "/" + current.id, body) : api.post(base, body);
  }, onSuccess: () => { setPassword(""); setPassphrase(""); changed(); close(); } });
  const field = (key: keyof Config, value: string | boolean | null) => setConfig((old) => ({ ...old, [key]: value }));
  return <form className="space-y-4 rounded-lg border p-4" onSubmit={(event) => { event.preventDefault(); mutation.mutate(); }}>
    <h2 className="font-semibold">{current ? "Edit GitOps application" : "New GitOps application"}</h2>
    <div className="grid gap-3 sm:grid-cols-2">
      <label className="space-y-1 text-sm">Application name<Input required maxLength={63} value={name} onChange={(e) => setName(e.target.value)} /></label>
      <label className="space-y-1 text-sm">Git repository URL<Input required type="text" placeholder="https://github.com/team/repository.git" value={config.url} onChange={(e) => { const value = e.target.value; setConfig((old) => ({ ...old, url: value, ca_pem: null, ssh_host_key: null })); setMode(value.startsWith("ssh://") || /^[^@]+@[^:]+:/.test(value) ? "ssh" : "basic"); setPassword(""); setPassphrase(""); if (value.startsWith("ssh://") || /^[^@]+@[^:]+:/.test(value)) setAuthenticated(true); }} /></label>
      <label className="space-y-1 text-sm">Branch<Input required value={config.branch} onChange={(e) => field("branch", e.target.value)} /></label>
      <label className="space-y-1 text-sm">Manifest directory<Input required value={config.path} onChange={(e) => field("path", e.target.value)} /></label>
      {current ? <p className="text-sm">Destination namespace: {config.namespace}</p> : <Namespace value={config.namespace} names={namespaces} change={(v) => field("namespace", v)} />}
    </div>
    <label className="flex items-center gap-2 text-sm"><input type="checkbox" disabled={ssh} checked={authenticated} onChange={(e) => setAuthenticated(e.target.checked)} />Use Git repository authentication</label>
    {authenticated && <div className="grid gap-3 sm:grid-cols-3">
      <label className="space-y-1 text-sm">Git authentication method<select className="h-9 w-full rounded border bg-background px-2" value={mode} onChange={(e) => setMode(e.target.value)}>{ssh ? <option value="ssh">SSH private key</option> : <><option value="basic">Username and password/token</option><option value="bearer">Bearer token</option></>}</select></label>
      {mode === "basic" && <label className="space-y-1 text-sm">Git username<Input required={Boolean(password) || !current?.authenticated || current.configuration.url !== config.url} autoComplete="off" value={username} onChange={(e) => setUsername(e.target.value)} /></label>}
      {ssh ? <><label className="space-y-1 text-sm">SSH private key<Textarea required={Boolean(passphrase) || !current?.authenticated || current.configuration.url !== config.url} autoComplete="off" spellCheck={false} value={password} onChange={(e) => setPassword(e.target.value)} /></label><label className="space-y-1 text-sm">SSH key passphrase (optional)<Input type="password" autoComplete="new-password" value={passphrase} onChange={(e) => setPassphrase(e.target.value)} /></label></> : <label className="space-y-1 text-sm">Git password or token<Input type="password" required={!current?.authenticated || current.configuration.url !== config.url} autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} /></label>}
      <p className="text-xs text-muted-foreground sm:col-span-3">Credentials are encrypted and never returned. Leave blank to preserve them, re-enter to rotate, or disable HTTPS authentication to remove it. SSH requires a key; switch to anonymous HTTPS to remove SSH credentials. Re-enter the key when changing its passphrase.</p>
    </div>}
    <details open={ssh}><summary className="cursor-pointer text-sm">Git endpoint trust settings</summary><div className="mt-3 space-y-3">
      <label className="flex items-center gap-2 text-sm"><input type="checkbox" disabled={!admin} checked={config.allow_private_network} onChange={(e) => field("allow_private_network", e.target.checked)} />Allow private Git endpoints and custom ports</label>
      {ssh ? <label className="block space-y-1 text-sm">Trusted SSH host public key<Textarea required readOnly={!admin} placeholder="ssh-ed25519 AAAA…" value={config.ssh_host_key ?? ""} onChange={(e) => field("ssh_host_key", e.target.value || null)} /></label> : <label className="block space-y-1 text-sm">Git CA certificate bundle<Textarea readOnly={!admin} value={config.ca_pem ?? ""} onChange={(e) => field("ca_pem", e.target.value || null)} /></label>}
      <p className="text-xs text-muted-foreground">Administrators control endpoint trust. Verify SSH host keys through a trusted channel before saving; SSH never accepts unknown or changed keys automatically. TLS verification remains enabled; redirects, loopback, link-local and metadata endpoints are blocked.</p>
    </div></details>
    <div className="flex gap-2"><Button disabled={mutation.isPending || !config.namespace}>Save application</Button><Button type="button" variant="outline" onClick={close}>Cancel application edit</Button></div>
    {mutation.isError && <ErrorState error={mutation.error} />}
  </form>;
}

function ResourceList({ resources }: { resources: Resource[] }) {
  return <ul className="max-h-72 space-y-1 overflow-auto text-sm">{resources.map((r, i) => <li key={i}>{r.action ? `${r.action} · ` : ""}{r.kind} · {r.namespace}/{r.name}{r.reason ? ` · ${r.reason.replaceAll("_", " ")}` : ""}</li>)}</ul>;
}

function AutomationControls({ app, path, changed, requested }: { app: Application; path: string; changed: () => void; requested: () => void }) {
  const admin = useCan("organization.manage");
  const write = useCan("clusters.write");
  const [policy, setPolicy] = useState<Automation>(app.automation);
  const [confirm, setConfirm] = useState(false);
  const save = useMutation({ mutationFn: () => api.put(path + "/automation", { ...policy, confirm: app.name, generation: app.generation }), onSuccess: () => { setConfirm(false); changed(); } });
  const pause = useMutation({ mutationFn: () => api.post(path + "/automation/pause"), onSuccess: changed });
  const reconcile = useMutation({ mutationFn: () => api.post(path + "/automation/reconcile"), onSuccess: () => { requested(); changed(); } });
  return <details className="space-y-3 rounded border p-3"><summary className="cursor-pointer text-sm font-medium">Automatic reconciliation · {app.automation.enabled ? "Enabled" : "Paused"}</summary>
    <p className="text-xs text-muted-foreground">{app.automation.enabled ? `Checks every ${app.automation.interval_seconds} seconds. Pruning ${app.automation.prune ? "enabled" : "disabled"}.` : "No automatic mutations are scheduled."}{app.next_reconcile_at ? ` Next check due ${new Date(app.next_reconcile_at).toLocaleString()}.` : ""}</p>
    <p className="text-xs text-muted-foreground">Source edits pause automation. Policy changes invalidate pending reviews. Pause stops queued work and further mutations at the next checkpoint; accepted Kubernetes requests may finish.</p>
    <div className="flex flex-wrap gap-2">{write && app.automation.enabled && <><Button variant="outline" disabled={pause.isPending} onClick={() => pause.mutate()}>Pause reconciliation {app.name}</Button><Button variant="outline" disabled={reconcile.isPending} onClick={() => reconcile.mutate()}>Reconcile now {app.name}</Button></>}</div>
    {reconcile.isSuccess && <p className="text-sm">Reconciliation requested; the scheduler checks every 30 seconds.</p>}
    {admin && <div className="space-y-3 border-t pt-3">
      <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={policy.enabled} onChange={(e) => setPolicy({ ...policy, enabled: e.target.checked })} />Enable automatic reconciliation</label>
      <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={policy.prune} onChange={(e) => setPolicy({ ...policy, prune: e.target.checked, allow_empty: e.target.checked && policy.allow_empty })} />Automatically prune removed resources</label>
      {policy.prune && <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={policy.allow_empty} onChange={(e) => setPolicy({ ...policy, allow_empty: e.target.checked })} />Allow empty sources to prune all eligible owned resources</label>}
      <label className="block space-y-1 text-sm">Reconciliation interval (seconds)<Input type="number" min={60} max={3600} value={policy.interval_seconds} onChange={(e) => setPolicy({ ...policy, interval_seconds: Number(e.target.value) })} /></label>
      <p className="text-xs text-muted-foreground">Pruning deletes only tracked resources with matching ownership and UID, after supported desired workloads are ready. PVCs, StatefulSets, custom resources and retention annotations are protected. Unreachable or invalid repositories never trigger deletion.</p>
      <Button variant="outline" disabled={save.isPending || !Number.isInteger(policy.interval_seconds) || policy.interval_seconds < 60 || policy.interval_seconds > 3600} onClick={() => setConfirm(true)}>Save automation policy {app.name}</Button>
    </div>}
    {save.isError && <ErrorState error={save.error} />}{pause.isError && <ErrorState error={pause.error} />}{reconcile.isError && <ErrorState error={reconcile.error} />}
    <ConfirmDialog key={String(confirm)} open={confirm} onOpenChange={setConfirm} title="Approve GitOps automation policy" description={policy.enabled ? `Authorize recurring updates from this repository, branch, path and namespace. ${policy.prune ? "Removed eligible owned resources may be deleted automatically." : "Resources removed from Git will be retained."}${policy.allow_empty ? " An empty manifest directory may delete all eligible owned resources." : " Empty manifest sets will stop reconciliation."}` : "Pause automation and invalidate pending reviews and further sync mutations for this application."} confirmLabel="Approve automation policy" confirmText={app.name} pending={save.isPending} onConfirm={() => save.mutate()} />
  </details>;
}

function ApplicationCard({ app, base, edit, changed }: { app: Application; base: string; edit: () => void; changed: () => void }) {
  const write = useCan("clusters.write");
  const [review, setReview] = useState<Review | null>(null);
  const [prune, setPrune] = useState(false);
  const [allowEmpty, setAllowEmpty] = useState(false);
  const draft = useRef(0);
  const resetReview = () => { draft.current += 1; setReview(null); };
  const [confirmation, setConfirmation] = useState<"sync" | "forget" | null>(null);
  const [operation, setOperation] = useState<string | null>(null);
  const [showHistory, setShowHistory] = useState(false);
  const path = base + "/" + app.id;
  const history = useQuery({ queryKey: ["gitops-history", app.id], queryFn: () => api.get<Review[]>(path + "/history"), enabled: showHistory, refetchInterval: showHistory ? 10000 : false });
  const preview = useMutation({ mutationFn: () => api.post<Review>(path + "/review", { prune, allow_empty: allowEmpty }), onMutate: () => { setReview(null); return draft.current; }, onSuccess: (result, _variables, generation) => { if (generation !== draft.current) return; setReview(result); changed(); void history.refetch(); } });
  const sync = useMutation({ mutationFn: () => api.post<{ id: string }>(path + "/sync", { review_id: review?.id, confirm: app.name }), onSuccess: (result) => { setOperation(result.id); setReview(null); setConfirmation(null); changed(); void history.refetch(); } });
  const forget = useMutation({ mutationFn: () => api.post(path + "/forget", { confirm: app.name }), onSuccess: () => { setConfirmation(null); changed(); } });
  return <section className="space-y-3 rounded-lg border p-4">
    <div className="flex flex-wrap items-center justify-between gap-2"><h2 className="font-semibold">{app.name}</h2><span className="text-sm">{app.observation.status ?? "Not checked"}</span></div>
    <p className="break-all text-sm text-muted-foreground">{app.configuration.url} · {app.configuration.branch} · {app.configuration.path} → {app.configuration.namespace} · {app.authenticated ? "Credentials configured" : "Anonymous"}</p>
    {app.observation.checked_at && <p className="text-xs text-muted-foreground">Last drift check: {new Date(app.observation.checked_at).toLocaleString()} · {app.observation.commit}</p>}
    {app.observation.last_synced_commit && <p className="text-xs text-muted-foreground">Last applied commit: {app.observation.last_synced_commit}</p>}
    <div className="flex flex-wrap gap-2">
      {write && <><Button variant="outline" disabled={preview.isPending} onClick={() => preview.mutate()}>{preview.isPending ? "Checking repository…" : `Review latest commit for ${app.name}`}</Button><Button variant="outline" onClick={edit}>Edit {app.name}</Button><Button variant="outline" onClick={() => setConfirmation("forget")}>Remove registration {app.name}</Button></>}
      <Button variant="outline" onClick={() => setShowHistory(!showHistory)}>History {app.name}</Button>
    </div>
    {write && <div className="space-y-2 text-sm"><label className="flex items-center gap-2"><input type="checkbox" checked={prune} onChange={(e) => { setPrune(e.target.checked); if (!e.target.checked) setAllowEmpty(false); resetReview(); }} />Include pruning in manual review</label>{prune && <label className="flex items-center gap-2"><input type="checkbox" checked={allowEmpty} onChange={(e) => { setAllowEmpty(e.target.checked); resetReview(); }} />Allow an empty manifest set in this review</label>}</div>}
    <AutomationControls app={app} path={path} changed={changed} requested={() => setOperation(null)} />
    {app.observation.error && <p role="alert" className="text-sm text-destructive">{app.observation.error}</p>}
    {preview.isError && <ErrorState error={preview.error} />}
    {review && <div className="space-y-3 rounded border p-3"><h3 className="font-medium">Reviewed Git commit</h3><p className="break-all font-mono text-xs">{review.commit}</p><p className="text-xs text-muted-foreground">Expires {new Date(review.expires_at).toLocaleTimeString()}. Resource names and actions only; manifests and Secret values are withheld.</p><ResourceList resources={review.resources} />{Boolean(review.prune?.length) && <div className="space-y-1"><h4 className="text-sm font-semibold">Prune plan</h4><ResourceList resources={review.prune ?? []} /></div>}{review.retained.length > 0 && <div><p className="text-sm">Resources absent from this commit will be retained:</p><ResourceList resources={review.retained} /></div>}<Button disabled={review.resources.some((r) => r.action === "conflict") || Boolean(review.prune?.some((r) => r.action === "blocked"))} onClick={() => setConfirmation("sync")}>Sync reviewed commit</Button>{(review.resources.some((r) => r.action === "conflict") || review.prune?.some((r) => r.action === "blocked")) && <p className="text-sm text-destructive">Resolve resource ownership, identity or field conflicts and review again before syncing. Existing managers will not be overridden.</p>}</div>}
    {sync.isError && <ErrorState error={sync.error} />}{forget.isError && <ErrorState error={forget.error} />}
    <Phase6Operation id={operation ?? app.observation.reconcile_operation_id ?? null} />
    {showHistory && <div className="space-y-2"><h3 className="font-medium">Review and sync history</h3>{history.isPending && <p>Loading history…</p>}{history.isError && <ErrorState error={history.error} />}{history.data?.length === 0 && <p className="text-sm">No revisions reviewed yet.</p>}{history.data?.map((entry) => <div key={entry.id} className="border-t pt-2 text-sm"><p className="break-all font-mono text-xs">{entry.commit}</p><p>{entry.automatic ? "Automatic · " : "Manual · "}{entry.completed?.status ?? (entry.operation_id ? "Sync submitted; see operation" : "Reviewed")}{entry.completed?.pruned ? ` · ${entry.completed.pruned} pruned` : ""}{entry.created_at ? ` · ${new Date(entry.created_at).toLocaleString()}` : ""}</p>{entry.operation_id && <Link className="underline" href={`/operations/${entry.operation_id}`}>View sync operation</Link>}</div>)}</div>}
    <ConfirmDialog key={`${confirmation}:${review?.id ?? "none"}`} open={confirmation !== null} onOpenChange={(open) => { if (!open) setConfirmation(null); }} title={confirmation === "forget" ? "Remove GitOps registration" : "Sync reviewed Git commit"} description={confirmation === "forget" ? "Remove application configuration and saved credentials. Kubernetes resources remain in place, including their ownership annotations." : review?.prune_enabled ? "Apply the reviewed commit and delete the eligible owned resources listed in the prune plan. Protected resources remain. This may disrupt workloads." : "Apply the reviewed commit to this namespace. Changes can restart workloads. Resources removed from Git will be retained."} confirmLabel={confirmation === "forget" ? "Remove registration" : "Execute sync"} confirmText={app.name} pending={sync.isPending || forget.isPending} onConfirm={() => confirmation === "forget" ? forget.mutate() : sync.mutate()} />
  </section>;
}

function ClusterGitOps({ id }: { id: string }) {
  const client = useQueryClient();
  const write = useCan("clusters.write");
  const [editing, setEditing] = useState<Application | "new" | null>(null);
  const base = `/clusters/${id}/gitops/applications`;
  const applications = useQuery({ queryKey: ["gitops-applications", id], queryFn: () => api.get<Application[]>(base), refetchInterval: 10000 });
  const namespaces = useClusterNamespaces(id);
  const changed = () => { void client.invalidateQueries({ queryKey: ["gitops-applications", id] }); };
  return <div className="space-y-4"><p className="text-sm text-muted-foreground">Commit-pinned Kubernetes YAML delivery into one existing namespace. Use confirmed manual syncs or administrator-approved automatic reconciliation. Pruning is a separate opt-in with ownership, identity and readiness checks. Helm/Kustomize rendering and Argo CD/Flux integration remain separate work.</p>
    <div className="flex gap-2"><Button variant="outline" onClick={() => applications.refetch()}>Refresh applications</Button>{write && <Button onClick={() => setEditing("new")}>Add GitOps application</Button>}</div>
    {editing && <ApplicationForm key={editing === "new" ? "new" : editing.id} base={base} current={editing === "new" ? undefined : editing} namespaces={namespaces.data?.map((n) => n.name) ?? []} close={() => setEditing(null)} changed={changed} />}
    {namespaces.isError && <ErrorState error={namespaces.error} />}{applications.isPending && <p>Loading GitOps applications…</p>}{applications.isError && <ErrorState error={applications.error} onRetry={() => applications.refetch()} />}
    {applications.data?.length === 0 && <EmptyState title="No GitOps applications" description="Connect a Git repository and choose the directory of manifests to manage." />}
    {applications.data?.map((app) => <ApplicationCard key={`${app.id}:${app.generation}`} app={app} base={base} edit={() => setEditing(app)} changed={changed} />)}
  </div>;
}

export function GitOpsPage() {
  return <div className="space-y-5"><PageHeader eyebrow="Workloads" title="GitOps" description="Review Git commits, sync Kubernetes manifests and inspect drift." /><ClusterScope>{(id) => <ClusterGitOps key={id} id={id} />}</ClusterScope></div>;
}
