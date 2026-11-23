"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, ChevronsUpDown } from "lucide-react";
import { useRef, useState } from "react";

import { ConfirmDialog } from "@/components/common/confirm-dialog";
import { EmptyState, ErrorState, PageHeader } from "@/components/common/page";
import { useCan } from "@/components/providers/auth";
import { Button } from "@/components/ui/button";
import { Command, CommandEmpty, CommandInput, CommandItem, CommandList } from "@/components/ui/command";
import { Input } from "@/components/ui/input";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Textarea } from "@/components/ui/textarea";
import { ClusterScope } from "@/features/kubernetes/shared";
import { Phase6Operation } from "@/features/kubernetes/phase6-operation";
import { api, apiRequest } from "@/lib/api/client";
import { useClusterNamespaces } from "@/lib/api/kube";

type Repo = { name: string; url: string; auth_id?: string | null; allow_private_network?: boolean; ca_pem?: string | null; token_service_url?: string | null; authentication?: { mode: string; username: string; password: string }; clear_credentials?: boolean };
type Release = { name: string; namespace: string; revision: string | number; status: string; chart: string; app_version: string; updated: string };
type Revision = { revision: number; status: string; chart: string; app_version: string; updated: string };
type Action = "install" | "upgrade" | "rollback" | "uninstall";
type Edit = { action: Action; release?: Release; revision?: number };
type Chart = { name: string; versions: { version: string; description: string; app_version: string }[] };
type Plan = { id: string; expires_at: string; specification: { release: string; namespace: string; expected_revision: number; revision?: number; source?: { chart: string; version: string } }; resources: { kind: string; name: string; namespace: string }[]; notice: string };

function Choice({ label, value, options, change, optional = false }: { label: string; value: string; options: string[]; change: (v: string) => void; optional?: boolean }) {
  const [open, setOpen] = useState(false);
  return <div className="space-y-1 text-sm"><span>{label}</span>
    <Popover open={open} onOpenChange={setOpen}><PopoverTrigger asChild>
      <Button type="button" role="combobox" aria-label={label} aria-expanded={open} variant="outline" className="w-full justify-between">
        <span className="truncate">{value || (optional ? "All namespaces" : `Select ${label.toLowerCase()}`)}</span><ChevronsUpDown className="size-4 shrink-0" />
      </Button></PopoverTrigger><PopoverContent className="w-[var(--radix-popover-trigger-width)] p-0" align="start">
      <Command><CommandInput aria-label={`Search ${label.toLowerCase()}`} placeholder={`Search ${label.toLowerCase()}…`} /><CommandList>
        <CommandEmpty>No matching options</CommandEmpty>
        {optional && <CommandItem value="All namespaces" onSelect={() => { change(""); setOpen(false); }}>All namespaces</CommandItem>}
        {options.map((option) => <CommandItem key={option} value={option} onSelect={() => { change(option); setOpen(false); }}>{option}{value === option && <Check className="ml-auto size-4" />}</CommandItem>)}
      </CommandList></Command></PopoverContent></Popover>
  </div>;
}

function Repositories({ id, repos, changed }: { id: string; repos: Repo[]; changed: () => void }) {
  const canWrite = useCan("clusters.write");
  const admin = useCan("organization.manage");
  const [editing, setEditing] = useState<Repo | null>(null);
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [authenticated, setAuthenticated] = useState(false);
  const [mode, setMode] = useState("basic");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [privateNetwork, setPrivateNetwork] = useState(false);
  const [ca, setCa] = useState("");
  const [tokenService, setTokenService] = useState("");
  const clear = () => { setEditing(null); setName(""); setUrl(""); setAuthenticated(false); setMode("basic"); setUsername(""); setPassword(""); setPrivateNetwork(false); setCa(""); setTokenService(""); };
  const save = useMutation({ mutationFn: (repositories: Repo[]) => api.put(`/clusters/${id}/helm/repositories`, { repositories }), onSuccess: () => { clear(); changed(); } });
  const edit = (repo: Repo) => { setEditing(repo); setName(repo.name); setUrl(repo.url); setAuthenticated(Boolean(repo.auth_id)); setMode("basic"); setUsername(""); setPassword(""); setPrivateNetwork(Boolean(repo.allow_private_network)); setCa(repo.ca_pem ?? ""); setTokenService(repo.token_service_url ?? ""); save.reset(); };
  return <details className="rounded-lg border p-4"><summary className="cursor-pointer font-medium">Chart repositories</summary>
    <div className="mt-3 space-y-3">
      <p className="text-sm text-muted-foreground">HTTPS chart indexes or a full OCI chart URL, such as oci://registry.example.com/team/chart. Removing a source revokes its saved credentials; installed releases remain.</p>
      {!repos.length && <p className="text-sm">No chart repositories configured.</p>}
      {repos.map((repo) => <div key={repo.name} className="flex flex-wrap items-center justify-between gap-2 text-sm"><span>{repo.name} · {repo.url} · {repo.auth_id ? "Credentials configured" : "Anonymous"}</span>
        {canWrite && <div className="flex gap-2"><Button size="sm" variant="outline" onClick={() => edit(repo)}>Edit {repo.name}</Button><Button size="sm" variant="outline" disabled={save.isPending} onClick={() => save.mutate(repos.filter((r) => r.name !== repo.name))}>Remove {repo.name}</Button></div>}</div>)}
      {canWrite && <form className="space-y-3 rounded border p-3" onSubmit={(e) => {
        e.preventDefault();
        const entry: Repo = { name, url, auth_id: editing?.auth_id ?? null, allow_private_network: privateNetwork, ca_pem: ca || null, token_service_url: tokenService || null,
          clear_credentials: Boolean(editing?.auth_id && !authenticated),
          ...(authenticated && password ? { authentication: { mode, username, password } } : {}) };
        save.mutate(editing ? repos.map((r) => r.name === editing.name ? entry : r) : [...repos, entry]);
      }}>
        <div className="grid gap-3 sm:grid-cols-2">
          <label className="space-y-1 text-sm">Repository name<Input required disabled={Boolean(editing)} maxLength={63} value={name} onChange={(e) => setName(e.target.value)} /></label>
          <label className="space-y-1 text-sm">Repository URL<Input required type="url" placeholder="https://example.com/charts or oci://registry.example.com/team/chart" value={url} onChange={(e) => setUrl(e.target.value)} /></label>
        </div>
        <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={authenticated} onChange={(e) => setAuthenticated(e.target.checked)} />Use repository authentication</label>
        {authenticated && <div className="grid gap-3 sm:grid-cols-3">
          <label className="space-y-1 text-sm">Authentication method<select className="h-9 w-full rounded-md border bg-background px-2" value={mode} onChange={(e) => setMode(e.target.value)}><option value="basic">Username and password/token</option><option value="bearer">Bearer token</option></select></label>
          {mode === "basic" && <label className="space-y-1 text-sm">Repository username<Input required={Boolean(password) || !editing?.auth_id || editing.url !== url} autoComplete="off" value={username} onChange={(e) => setUsername(e.target.value)} /></label>}
          <label className="space-y-1 text-sm">Repository password or token<Input type="password" required={!editing?.auth_id || editing.url !== url} autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} /></label>
          <p className="text-xs text-muted-foreground sm:col-span-3">Saved credentials are encrypted and never returned. Leave credentials blank to preserve them; re-enter both fields to rotate Basic credentials. Disabling authentication removes the saved credential reference.</p>
        </div>}
        <details><summary className="cursor-pointer text-sm">Endpoint trust settings</summary><div className="mt-2 space-y-3">
          <label className="flex items-center gap-2 text-sm"><input type="checkbox" disabled={!admin} checked={privateNetwork} onChange={(e) => setPrivateNetwork(e.target.checked)} />Allow private network endpoints and custom TLS ports</label>
          <label className="block space-y-1 text-sm">Repository CA certificate bundle<Textarea readOnly={!admin} className="font-mono text-xs" value={ca} onChange={(e) => setCa(e.target.value)} placeholder="Optional PEM CA certificates; no private keys" /></label>
          <label className="block space-y-1 text-sm">Trusted OCI token service URL<Input readOnly={!admin} type="url" value={tokenService} onChange={(e) => setTokenService(e.target.value)} placeholder="Optional exact HTTPS token endpoint for another origin" /></label>
          <p className="text-xs text-muted-foreground">Organization/platform administrators control endpoint trust. TLS verification stays enabled. Loopback, metadata and link-local addresses are always blocked.</p>
        </div></details>
        <div className="flex gap-2"><Button disabled={save.isPending || (!editing && repos.length >= 20)}>{editing ? "Update repository" : "Add repository"}</Button>{editing && <Button type="button" variant="outline" onClick={clear}>Cancel repository edit</Button>}</div>
      </form>}
      {save.isError && <ErrorState error={save.error} />}
    </div>
  </details>;
}

function ReleaseForm({ id, edit, repos, namespaces, close, submitted }: { id: string; edit: Edit; repos: Repo[]; namespaces: string[]; close: () => void; submitted: () => void }) {
  const [name, setName] = useState(edit.release?.name ?? "");
  const [namespace, setNamespace] = useState(edit.release?.namespace ?? (namespaces.includes("default") ? "default" : ""));
  const [repo, setRepo] = useState("");
  const [chart, setChart] = useState("");
  const [version, setVersion] = useState("");
  const [searchDraft, setSearchDraft] = useState("");
  const [search, setSearch] = useState("");
  const [values, setValues] = useState("{}");
  const [reuse, setReuse] = useState(true);
  const [hooks, setHooks] = useState(false);
  const [timeout, setTimeout] = useState(300);
  const [review, setReview] = useState<Plan | null>(null);
  const [confirm, setConfirm] = useState(false);
  const [operation, setOperation] = useState<string | null>(null);
  const generation = useRef(0);
  const client = useQueryClient();
  const base = `/clusters/${id}/helm`;
  const chartAction = edit.action === "install" || edit.action === "upgrade";
  const charts = useQuery({ queryKey: ["helm-charts", id, repo, search], queryFn: () => apiRequest<{ items: Chart[]; truncated: boolean }>(base + "/charts", { query: { repository: repo, search } }), enabled: Boolean(repo), retry: false });
  const ociChart = repos.find((r) => r.name === repo)?.url.startsWith("oci://") ? repos.find((r) => r.name === repo)?.url.split("/").at(-1) ?? "" : "";
  const versions = charts.data?.items.find((r) => r.name === chart)?.versions ?? [];
  const defaults = useMutation({ mutationFn: (observed: number) => {
    if (observed !== generation.current) throw new Error("The chart selection changed; load defaults again.");
    return apiRequest<{ values_yaml: string }>(base + "/charts/values", { query: { repository: repo, chart, version } });
  }, onSuccess: (r, observed) => { if (observed === generation.current) { generation.current += 1; setValues(r.values_yaml); setReview(null); } } });
  const preview = useMutation({ mutationFn: (observed: number) => {
    if (observed !== generation.current) throw new Error("The form changed; review again.");
    return api.post<Plan>(base + "/preview", { action: edit.action, release: name, namespace, repository: repo || null, chart: chart || null, version: version || null, values_yaml: chartAction ? values : "{}", reuse_values: reuse, hooks, revision: edit.revision ?? null, timeout_seconds: timeout });
  }, onSuccess: (plan, observed) => { if (observed === generation.current) setReview(plan); } });
  const execute = useMutation({ mutationFn: () => api.post<{ id: string }>(base + "/execute", { plan_id: review?.id, confirm: edit.action === "install" ? null : name }), onSuccess: (op) => { setConfirm(false); setOperation(op.id); setReview(null); submitted(); void client.invalidateQueries({ queryKey: ["helm-releases", id] }); void client.invalidateQueries({ queryKey: ["helm-history", id] }); } });
  const reset = () => { generation.current += 1; setReview(null); preview.reset(); execute.reset(); };
  return <section className="space-y-4 rounded-lg border p-4">
    <div className="flex items-center justify-between"><h2 className="font-semibold capitalize">{edit.action} release</h2><Button variant="ghost" onClick={close}>Close release form</Button></div>
    <div className="grid gap-3 sm:grid-cols-2">
      <label className="space-y-1 text-sm">Release name<Input maxLength={53} value={name} disabled={edit.action !== "install"} onChange={(e) => { setName(e.target.value); reset(); }} /></label>
      <Choice label="Release namespace" value={namespace} options={edit.action === "install" ? namespaces : [namespace]} change={(v) => { setNamespace(v); reset(); }} />
    </div>
    {chartAction && <>
      <div className="grid gap-3 sm:grid-cols-3">
        <Choice label="Repository" value={repo} options={repos.map((r) => r.name)} change={(v) => { setRepo(v); setChart(""); setVersion(""); setSearch(""); setSearchDraft(""); reset(); }} />
        <Choice label="Chart" value={chart} options={ociChart ? [ociChart] : charts.data?.items.map((r) => r.name) ?? []} change={(v) => { setChart(v); setVersion(""); reset(); }} />
        <Choice label="Chart version" value={version} options={versions.map((v) => v.version)} change={(v) => { setVersion(v); reset(); }} />
      </div>
      <form className="flex items-end gap-2" onSubmit={(e) => { e.preventDefault(); setSearch(searchDraft); setChart(""); setVersion(""); reset(); }}>
        <label className="flex-1 space-y-1 text-sm">Filter chart catalog<Input maxLength={100} value={searchDraft} onChange={(e) => setSearchDraft(e.target.value)} /></label>
        <Button variant="outline" disabled={!repo || charts.isFetching}>Search charts</Button>
      </form>
      {ociChart && <label className="block space-y-1 text-sm">Known OCI version<Input value={version} placeholder="Use an explicit version if tag listing is unavailable" onChange={(e) => { setVersion(e.target.value); reset(); }} /></label>}
      {charts.isFetching && <p className="text-sm">Loading chart catalog…</p>}
      {charts.isError && <ErrorState error={charts.error} onRetry={() => charts.refetch()} />}
      {charts.data?.truncated && <p className="text-sm">Catalog is limited to 500 charts. Filter the chart catalog to narrow results.</p>}
      {version && <p className="text-sm text-muted-foreground">{versions.find((v) => v.version === version)?.description}</p>}
      <Button variant="outline" disabled={!version || defaults.isPending} onClick={() => defaults.mutate(generation.current)}>Load chart defaults</Button>
      {defaults.isError && <ErrorState error={defaults.error} />}
      <label className="block space-y-1 text-sm">Values YAML<Textarea className="min-h-64 font-mono text-xs" value={values} onChange={(e) => { setValues(e.target.value); reset(); }} spellCheck={false} /></label>
      <p className="text-xs text-muted-foreground">Submitted values are encrypted at rest. Installed values and Secret contents are not displayed in release history or operation logs.</p>
      {edit.action === "upgrade" && <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={reuse} onChange={(e) => { setReuse(e.target.checked); reset(); }} />Reuse existing values, applying the overrides above</label>}
    </>}
    {edit.action === "rollback" && <p className="text-sm">Roll back revision {edit.release?.revision} to revision {edit.revision}. Helm creates a new history revision.</p>}
    {edit.action === "uninstall" && <p className="text-sm">Remove this release and its chart-managed resources. CRDs, retained volumes and resources marked to keep may remain.</p>}
    <div className="flex flex-wrap items-center gap-4">
      <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={hooks} onChange={(e) => { setHooks(e.target.checked); reset(); }} />Run chart hooks (can execute Jobs)</label>
      <label className="flex items-center gap-2 text-sm">Wait timeout (seconds)<Input className="w-24" type="number" min={30} max={600} value={timeout} onChange={(e) => { setTimeout(Number(e.target.value)); reset(); }} /></label>
    </div>
    <Button disabled={!name || !namespace || (chartAction && !version) || preview.isPending || execute.isPending} onClick={() => { setReview(null); preview.mutate(generation.current); }}>Review Helm plan</Button>
    {preview.isError && <ErrorState error={preview.error} />}
    {review && <div className="space-y-3 rounded border p-3">
      <h3 className="font-medium">Reviewed Helm plan</h3>
      <p className="text-sm">{edit.action} {namespace}/{name} · current revision {review.specification.expected_revision}{review.specification.source && ` · ${review.specification.source.chart} ${review.specification.source.version}`}</p>
      <p className="text-xs text-muted-foreground">Review expires {new Date(review.expires_at).toLocaleTimeString()}. {review.notice}</p>
      <p className="text-sm">{review.resources.length} rendered resources{edit.action === "upgrade" && reuse ? " with the supplied overrides and chart defaults; execution also reuses installed values" : ""}</p>
      {review.resources.length > 0 && <ul className="max-h-48 overflow-auto font-mono text-xs">{review.resources.map((r, i) => <li key={i}>{r.kind} {r.namespace}/{r.name}</li>)}</ul>}
      <Button disabled={execute.isPending} onClick={() => edit.action === "install" ? execute.mutate() : setConfirm(true)}>{edit.action === "install" ? "Install reviewed release" : `Confirm ${edit.action}`}</Button>
    </div>}
    {execute.isError && <ErrorState error={execute.error} />}
    <Phase6Operation id={operation} />
    <ConfirmDialog open={confirm} onOpenChange={setConfirm} title={`Confirm Helm ${edit.action}?`} description={<><p>This changes {namespace}/{name} and can interrupt workloads or remove chart-managed resources. Proceed only after reviewing the selected revision and values.</p>{execute.isError && <ErrorState error={execute.error} />}</>} confirmText={name} confirmLabel={`Execute ${edit.action}`} pending={execute.isPending} onConfirm={() => execute.mutate()} />
  </section>;
}

function ClusterHelm({ id }: { id: string }) {
  const canWrite = useCan("clusters.write");
  const client = useQueryClient();
  const [namespace, setNamespace] = useState("");
  const [edit, setEdit] = useState<Edit | null>(null);
  const [selected, setSelected] = useState<Release | null>(null);
  const namespaces = useClusterNamespaces(id);
  const repos = useQuery({ queryKey: ["helm-repos", id], queryFn: () => api.get<Repo[]>(`/clusters/${id}/helm/repositories`) });
  const releases = useQuery({ queryKey: ["helm-releases", id, namespace], queryFn: () => apiRequest<{ items: Release[]; truncated: boolean }>(`/clusters/${id}/helm/releases`, { query: { namespace } }), refetchInterval: 10000, retry: false });
  const history = useQuery({ queryKey: ["helm-history", id, selected?.namespace, selected?.name], queryFn: () => api.get<Revision[]>(`/clusters/${id}/helm/releases/${selected?.namespace}/${selected?.name}/history`), enabled: Boolean(selected), refetchInterval: 10000, retry: false });
  const current = releases.data?.items.find((r) => r.name === selected?.name && r.namespace === selected?.namespace) ?? selected;
  return <div className="space-y-4">
    {repos.data && <Repositories id={id} repos={repos.data} changed={() => { void client.invalidateQueries({ queryKey: ["helm-repos", id] }); void client.invalidateQueries({ queryKey: ["helm-charts", id] }); }} />}
    {repos.isError && <ErrorState error={repos.error} onRetry={() => repos.refetch()} />}
    <div className="flex flex-wrap items-end gap-3"><div className="min-w-60"><Choice label="Namespace filter" value={namespace} options={namespaces.data?.map((n) => n.name) ?? []} optional change={setNamespace} /></div>
      <Button variant="outline" onClick={() => { void releases.refetch(); if (selected) void history.refetch(); }}>Refresh releases</Button>
      {canWrite && <Button onClick={() => setEdit({ action: "install" })}>Install Helm release</Button>}
    </div>
    {namespaces.isError && <ErrorState error={namespaces.error} onRetry={() => namespaces.refetch()} />}
    {edit && <ReleaseForm key={JSON.stringify(edit)} id={id} edit={edit} repos={repos.data ?? []} namespaces={namespaces.data?.map((n) => n.name) ?? []} close={() => setEdit(null)} submitted={() => setSelected(null)} />}
    {releases.isPending && <p>Loading Helm releases…</p>}
    {releases.isError && <ErrorState error={releases.error} onRetry={() => releases.refetch()} />}
    {releases.data && <>
      {releases.data.truncated && <p>Showing the first 256 releases. Select a namespace to narrow the inventory.</p>}
      {!releases.data.items.length ? <EmptyState title="No Helm releases" description="Select a namespace or install a chart to create a release." /> : <div className="overflow-auto rounded-lg border"><table className="w-full text-left text-sm"><thead><tr>{["Release", "Namespace", "Revision", "Status", "Chart", "Actions"].map((h) => <th className="p-3" key={h}>{h}</th>)}</tr></thead><tbody>
        {releases.data.items.map((r) => <tr className="border-t" key={`${r.namespace}/${r.name}`}><td className="p-3 font-medium">{r.name}</td><td className="p-3">{r.namespace}</td><td className="p-3">{r.revision}</td><td className="p-3">{r.status}</td><td className="p-3">{r.chart}</td><td className="flex flex-wrap gap-2 p-3">
          <Button size="sm" variant="outline" onClick={() => setSelected(r)}>History {r.name}</Button>
          {canWrite && <><Button size="sm" variant="outline" onClick={() => setEdit({ action: "upgrade", release: r })}>Upgrade {r.name}</Button><Button size="sm" variant="outline" onClick={() => setEdit({ action: "uninstall", release: r })}>Uninstall {r.name}</Button></>}
        </td></tr>)}
      </tbody></table></div>}
    </>}
    {selected && <section className="space-y-2 rounded-lg border p-4"><h2 className="font-semibold">History: {selected.namespace}/{selected.name}</h2>
      {history.isPending && <p>Loading release history…</p>}{history.isError && <ErrorState error={history.error} />}
      {history.data?.map((r) => <div className="flex flex-wrap items-center justify-between gap-2 border-t py-2 text-sm" key={r.revision}><span>Revision {r.revision} · {r.status} · {r.chart} · {r.updated}</span>
        {canWrite && current && r.revision < Number(current.revision) && ["deployed", "superseded"].includes(r.status) && <Button size="sm" variant="outline" onClick={() => setEdit({ action: "rollback", release: current, revision: r.revision })}>Roll back to revision {r.revision}</Button>}
      </div>)}
    </section>}
  </div>;
}

export function HelmPage() {
  return <div className="space-y-5"><PageHeader eyebrow="Workloads" title="Helm releases" description="Browse charts and manage release installations, upgrades, rollbacks and removal." /><ClusterScope>{(id) => <ClusterHelm key={id} id={id} />}</ClusterScope></div>;
}
