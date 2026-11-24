"use client";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { parse, stringify } from "yaml";
import { ConfirmDialog } from "@/components/common/confirm-dialog";
import { EmptyState, ErrorState, PageHeader } from "@/components/common/page";
import { SearchableChoice } from "@/components/common/searchable-choice";
import { YamlEditor } from "@/components/common/yaml-editor";
import { useCan } from "@/components/providers/auth";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ClusterScope } from "@/features/kubernetes/shared";
import { api, ApiError } from "@/lib/api/client";
import { useClusterNamespaces } from "@/lib/api/kube";

import { DefinitionInstall, DefinitionManagement, type DefinitionResult } from "./definition-lifecycle";

type Condition = { type: string; status: string; last_transition_time?: string | null };
type Version = { name: string; served: boolean; storage: boolean };
type Definition = { managed?: boolean; name: string; uid: string; group: string; kind: string; plural: string; scope: "Namespaced" | "Cluster"; versions: Version[]; conditions: Condition[]; deletion_timestamp?: string | null };
type Resource = { name: string; namespace: string | null; uid: string; resource_version: string; generation?: number; observed_generation?: number; created_at?: string; deletion_timestamp?: string | null; conditions: Condition[]; finalizer_count: number; owner_count: number };
type SchemaField = { path: string; type: string; required: boolean; nullable: boolean; preserve_unknown: boolean; enum_count: number; validation_rule_count: number; minimum?: number; maximum?: number; minLength?: number; maxLength?: number; minItems?: number; maxItems?: number };
type Schema = { schema: { fields: SchemaField[]; truncated: boolean; has_schema: boolean }; template: string; subresources: string[] };
type Reviewed = { id: string; name: string; namespace: string | null; action: string; version: string; definition: string; expires_at: string; fields: string[]; notice: string };
type Result = { action: string; name: string; namespace: string | null; uid: string; status: string };
type Target = { definition: string; definition_uid: string; version: string; name: string; namespace: string | null; expected_uid?: string };

function ConditionList({ conditions }: { conditions: Condition[] }) {
  return conditions.length ? <ul className="space-y-1 text-sm">{conditions.map((c, i) => <li key={i}>{c.type}: <strong>{c.status}</strong>{c.last_transition_time ? ` · ${new Date(c.last_transition_time).toLocaleString()}` : ""}</li>)}</ul> : <p className="text-sm text-muted-foreground">No conditions reported.</p>;
}

function SchemaGuide({ base, definition, version, schema }: { base: string; definition: Definition; version: string; schema: Schema }) {
  const canReveal = useCan("secrets.reveal");
  const [filter, setFilter] = useState("");
  const [raw, setRaw] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const generation = useRef(0);
  useEffect(() => () => { generation.current += 1; }, []);
  const reveal = async () => { const request = ++generation.current; setBusy(true); setError(null); try { const data = await api.post<{ yaml: string }>(base + `/definitions/${definition.name}/reveal`, { version, definition_uid: definition.uid }); if (request === generation.current) setRaw(data.yaml); } catch (e) { if (request === generation.current) setError(e); } finally { if (request === generation.current) setBusy(false); } };
  const fields = schema.schema.fields.filter((field) => field.path.toLowerCase().includes(filter.toLowerCase()));
  return <details className="space-y-3 rounded-lg border p-4"><summary className="cursor-pointer font-medium">Schema guidance · {schema.schema.fields.length} fields</summary>
    <p className="text-sm text-muted-foreground">Required fields, types and bounds help author YAML. Kubernetes performs the full validation. Defaults, examples, enum values and validation expressions require an audited schema reveal.</p>
    {!schema.schema.has_schema && <p className="text-sm">No OpenAPI schema is published for this version.</p>}
    <Input aria-label="Search schema fields" placeholder="Search schema fields…" value={filter} onChange={(e) => setFilter(e.target.value)} />
    <div className="max-h-80 overflow-auto"><table className="w-full text-left text-sm"><thead><tr><th className="p-2">Field</th><th className="p-2">Type</th><th className="p-2">Required</th><th className="p-2">Constraints</th></tr></thead><tbody>{fields.map((field) => <tr className="border-t" key={field.path}><td className="p-2 font-mono text-xs">{field.path}</td><td className="p-2">{field.type}{field.nullable ? " · nullable" : ""}</td><td className="p-2">{field.required ? "Yes" : "No"}</td><td className="p-2">{Object.entries(field).filter(([key]) => ["minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems"].includes(key)).map(([key, value]) => `${key}: ${value}`).join(" · ")}{field.enum_count ? ` · ${field.enum_count} enum values` : ""}{field.validation_rule_count ? ` · ${field.validation_rule_count} validation rules` : ""}{field.preserve_unknown ? " · preserves unknown fields" : ""}</td></tr>)}</tbody></table></div>
    {schema.schema.truncated && <p className="text-sm">Schema guidance is limited to 500 fields and twelve nesting levels.</p>}
    <p className="text-xs text-muted-foreground">Subresources: {schema.subresources.join(", ") || "none"}</p>
    {canReveal && !raw && <Button variant="outline" disabled={busy} onClick={() => void reveal()}>Reveal schema YAML</Button>}
    {raw && canReveal && <div className="space-y-2"><Button variant="outline" onClick={() => { generation.current += 1; setRaw(null); setBusy(false); }}>Hide schema YAML</Button><YamlEditor value={raw} readOnly height="320px" /></div>}
    {error !== null && <ErrorState error={error} />}
  </details>;
}

function ResourceForm({ base, definition, version, target, initial, action, namespaces, defaultNamespace, expectedHash, close, done }: { base: string; definition: Definition; version: string; target?: Resource; initial: string; action: "create" | "update" | "delete"; namespaces: string[]; defaultNamespace?: string; expectedHash?: string; close: () => void; done: (result: Result) => void }) {
  const initialNamespace = target?.namespace ?? defaultNamespace ?? (namespaces.includes("default") ? "default" : namespaces[0] ?? "");
  const [namespace, setNamespace] = useState(initialNamespace);
  const [text, setText] = useState(() => { if (action !== "create") return initial; try { const doc = parse(initial); if (definition.scope === "Namespaced") doc.metadata.namespace = initialNamespace; return stringify(doc); } catch { return initial; } });
  const [review, setReview] = useState<Reviewed | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [confirm, setConfirm] = useState(false);
  const generation = useRef(0);
  useEffect(() => () => { generation.current += 1; }, []);
  let name = target?.name ?? "";
  if (action === "create") { try { const doc = parse(text); name = typeof doc?.metadata?.name === "string" ? doc.metadata.name : ""; } catch { name = ""; } }
  const reset = () => { generation.current += 1; setReview(null); setConfirm(false); setError(null); setBusy(false); };
  const runReview = async () => { const request = ++generation.current; setBusy(true); setReview(null); setError(null); try { const result = await api.post<Reviewed>(base + "/review", { definition: definition.name, definition_uid: definition.uid, version, name, namespace: definition.scope === "Namespaced" ? namespace : null, expected_uid: target?.uid, expected_hash: expectedHash, action, manifest: text }); if (request === generation.current) setReview(result); } catch (e) { if (request === generation.current) setError(e); } finally { if (request === generation.current) setBusy(false); } };
  const execute = async () => { const request = generation.current; setBusy(true); setError(null); try { const result = await api.post<Result>(base + "/execute", { review_id: review?.id, confirm: review?.name }); if (request === generation.current) done(result); } catch (e) { if (request === generation.current) setError(e); } finally { if (request === generation.current) setBusy(false); } };
  const fields = error instanceof ApiError && Array.isArray(error.details.fields) ? error.details.fields.filter((item): item is string => typeof item === "string") : [];
  return <section className="space-y-3 rounded-lg border p-4"><div className="flex items-center justify-between"><h3 className="font-semibold capitalize">{action} custom resource</h3><Button variant="ghost" onClick={close}>Close resource form</Button></div>
    {action === "create" && definition.scope === "Namespaced" && <SearchableChoice label="Destination namespace" value={namespace} options={namespaces.map((ns) => ({ value: ns, label: ns }))} change={(value) => { setNamespace(value); try { const doc = parse(text); doc.metadata.namespace = value; setText(stringify(doc)); } catch { /* Server validation reports invalid YAML. */ } reset(); }} />}
    <p className="text-sm text-muted-foreground">{definition.kind} · {version} · {definition.scope === "Cluster" ? "Cluster-scoped" : namespace}/{name || "(name required)"}. Status, finalizers, owner references and server metadata are read-only. Controllers may act on accepted changes.</p>
    {action !== "delete" && <div aria-label="Custom resource YAML editor"><YamlEditor value={text} onChange={(value) => { setText(value); reset(); }} height="360px" /></div>}
    <Button variant="outline" disabled={busy || !name || (definition.scope === "Namespaced" && !namespace) || text.length > 131072} onClick={() => void runReview()}>Review {action}</Button>
    {text.length > 131072 && <p className="text-sm text-destructive">The editor supports up to 128 KiB of YAML.</p>}
    {error !== null && <ErrorState error={error} />}{fields.length > 0 && <p className="text-sm text-destructive">Validation fields: {fields.join(", ")}</p>}
    {review && <div className="space-y-2 rounded border p-3"><h4 className="font-medium">Reviewed custom-resource action</h4><p className="text-sm">{review.action} · {review.definition} · {review.namespace || "(cluster)"}/{review.name} · {review.version}</p><p className="text-xs text-muted-foreground">Expires {new Date(review.expires_at).toLocaleTimeString()}. {review.notice}</p>{review.fields.length > 0 && <ul className="max-h-40 overflow-auto font-mono text-xs">{review.fields.map((field) => <li key={field}>{field}</li>)}</ul>}<Button disabled={busy} onClick={() => setConfirm(true)}>Confirm reviewed {action}</Button></div>}
    <ConfirmDialog key={`${confirm}:${review?.id ?? "none"}`} open={confirm} onOpenChange={setConfirm} title={`Confirm custom-resource ${action}`} description={action === "delete" ? "Request deletion of this resource and its dependents. Controller cleanup and finalizers can delay removal. External controllers may recreate it." : "Send the reviewed change to Kubernetes. Controllers may update workloads or infrastructure. The resource and CRD identity are checked again before execution."} confirmLabel={`Execute ${action}`} confirmText={review?.name} pending={busy} onConfirm={() => void execute()} />
  </section>;
}

function ResourceDetail({ base, clusterId, definition, version, item, namespaces, done, close }: { base: string; clusterId: string; definition: Definition; version: string; item: Resource; namespaces: string[]; done: (result: Result) => void; close: () => void }) {
  const write = useCan("clusters.write"); const canReveal = useCan("secrets.reveal");
  const target: Target = { definition: definition.name, definition_uid: definition.uid, version, name: item.name, namespace: item.namespace, expected_uid: item.uid };
  const detail = useQuery({ queryKey: ["custom-resource", clusterId, definition.name, version, item.namespace, item.name, item.uid], queryFn: () => api.get<{ summary: Resource; notice: string }>(base + "/resource", target), refetchInterval: 10000 });
  const [revealed, setRevealed] = useState<{ yaml: string; edit_yaml: string; content_hash: string } | null>(null);
  const [busy, setBusy] = useState(false); const [error, setError] = useState<unknown>(null); const [edit, setEdit] = useState<"update" | "delete" | null>(null);
  const generation = useRef(0); useEffect(() => () => { generation.current += 1; }, []);
  const reveal = async () => { const request = ++generation.current; setBusy(true); setError(null); try { const data = await api.post<{ yaml: string; edit_yaml: string; content_hash: string }>(base + "/reveal", target); if (request === generation.current) setRevealed(data); } catch (e) { if (request === generation.current) setError(e); } finally { if (request === generation.current) setBusy(false); } };
  const current = detail.data?.summary;
  return <section className="space-y-3 rounded-lg border p-4"><div className="flex items-center justify-between"><h2 className="font-semibold">Resource: {item.namespace ? `${item.namespace}/` : ""}{item.name}</h2><Button variant="ghost" onClick={close}>Close resource details</Button></div>
    {detail.isPending && <p>Loading resource details…</p>}{detail.isError && <ErrorState error={detail.error} onRetry={() => detail.refetch()} />}
    {current && <><p className="text-xs text-muted-foreground">UID {current.uid} · generation {current.generation ?? "—"} · observed {current.observed_generation ?? "—"} · {current.finalizer_count} finalizers · {current.owner_count} owners{current.deletion_timestamp ? " · Deletion pending" : ""}</p><ConditionList conditions={current.conditions} />
      <p className="text-sm text-muted-foreground">Custom-resource contents can contain sensitive values. Full YAML is revealed only by an explicit permission-checked, audited action.</p>
      <div className="flex flex-wrap gap-2">{canReveal && !revealed && <Button variant="outline" disabled={busy} onClick={() => void reveal()}>Reveal resource YAML</Button>}{canReveal && revealed && <Button variant="outline" onClick={() => { generation.current += 1; setRevealed(null); setEdit(null); setBusy(false); }}>Hide resource YAML</Button>}{write && canReveal && revealed && !current.deletion_timestamp && <Button variant="outline" onClick={() => setEdit("update")}>Edit resource YAML</Button>}{write && !current.deletion_timestamp && <Button variant="outline" onClick={() => setEdit("delete")}>Delete resource</Button>}</div>
      {revealed && canReveal && !edit && <YamlEditor value={revealed.yaml} readOnly height="360px" />}
      {edit && write && (edit === "delete" || canReveal) && <ResourceForm key={edit} base={base} definition={definition} version={version} target={current} initial={edit === "update" ? revealed?.edit_yaml ?? "" : ""} action={edit} expectedHash={edit === "update" ? revealed?.content_hash : undefined} namespaces={namespaces} close={() => setEdit(null)} done={(result) => { setRevealed(null); setEdit(null); done(result); }} />}
    </>}{error !== null && <ErrorState error={error} />}
  </section>;
}

function Instances({ id, definition, version, schema, available }: { id: string; definition: Definition; version: string; schema: Schema; available: boolean }) {
  const write = useCan("clusters.write"); const canReveal = useCan("secrets.reveal"); const client = useQueryClient(); const namespaces = useClusterNamespaces(id);
  const [namespace, setNamespace] = useState(""); const [token, setToken] = useState<string | null>(null); const [pages, setPages] = useState<(string | null)[]>([]); const [selected, setSelected] = useState<Resource | null>(null); const [create, setCreate] = useState(false); const [result, setResult] = useState<Result | null>(null);
  const base = `/clusters/${id}/custom-resources`;
  const list = useQuery({ queryKey: ["custom-resource-instances", id, definition.name, version, namespace, token], queryFn: () => api.get<{ items: Resource[]; continue_token: string | null; truncated: boolean; verbs: string[] }>(base + `/definitions/${definition.name}/resources`, { version, namespace: namespace || null, continue_token: token }), enabled: available, refetchInterval: 15000 });
  const names = namespaces.data?.map((ns) => ns.name) ?? [];
  const complete = (value: Result) => { setResult(value); setCreate(false); if (value.action === "create" && definition.scope === "Namespaced") { setNamespace(value.namespace ?? ""); setToken(null); setPages([]); } setSelected(null); void client.invalidateQueries({ queryKey: ["custom-resource-instances", id, definition.name] }); void client.invalidateQueries({ queryKey: ["custom-resource", id, definition.name] }); };
  if (!available) return <p className="rounded border p-4 text-sm">This version is not established and served. Its schema can be inspected; resource actions are unavailable.</p>;
  return <div className="space-y-4"><div className="flex flex-wrap items-end gap-3">{definition.scope === "Namespaced" ? <div className="min-w-64"><SearchableChoice label="Namespace filter" value={namespace} options={[{ value: "", label: "All namespaces" }, ...names.map((ns) => ({ value: ns, label: ns }))]} change={(value) => { setNamespace(value); setToken(null); setPages([]); setSelected(null); setCreate(false); setResult(null); }} /></div> : <p className="text-sm">Cluster-scoped resources</p>}<Button variant="outline" onClick={() => list.refetch()}>Refresh custom resources</Button>{write && list.data?.verbs.includes("create") && <Button onClick={() => { setCreate(true); setSelected(null); setResult(null); }}>Create custom resource</Button>}</div>
    {definition.scope === "Namespaced" && namespaces.isError && <ErrorState error={namespaces.error} />}
    {result && <p role="status" className="rounded border p-3 text-sm">{result.status.replaceAll("_", " ")} · {result.namespace || "(cluster)"}/{result.name}{result.action === "delete" ? ". Finalizers and controller cleanup may still be running." : ". Kubernetes accepted the change; controller status is shown separately."}</p>}
    {create && write && <ResourceForm key="create" base={base} definition={definition} version={version} initial={schema.template} action="create" defaultNamespace={namespace || undefined} namespaces={namespace ? [namespace, ...names.filter((n) => n !== namespace)] : names} close={() => setCreate(false)} done={complete} />}
    {list.isPending && <p>Loading custom resources…</p>}{list.isError && <ErrorState error={list.error} onRetry={() => list.refetch()} />}
    {list.data && <>{list.data.items.length === 0 ? <EmptyState title="No custom resources" description="No instances were returned for this version and namespace selection." /> : <div className="overflow-auto rounded-lg border"><table className="w-full text-left text-sm"><thead><tr><th className="p-3">Name</th><th className="p-3">Namespace</th><th className="p-3">Conditions</th><th className="p-3">State</th><th className="p-3" /></tr></thead><tbody>{list.data.items.map((item) => <tr className="border-t" key={item.uid}><td className="p-3 font-mono text-xs">{item.name}</td><td className="p-3">{item.namespace || "(cluster)"}</td><td className="p-3">{item.conditions.map((c) => `${c.type}: ${c.status}`).join(" · ") || "Not reported"}</td><td className="p-3">{item.deletion_timestamp ? "Deletion pending" : "Present"}</td><td className="p-3"><Button variant="outline" size="sm" onClick={() => { setSelected(item); setCreate(false); setResult(null); }}>Inspect {item.name}</Button></td></tr>)}</tbody></table></div>}
      <div className="flex gap-2"><Button variant="outline" disabled={!pages.length} onClick={() => { setToken(pages[pages.length - 1]); setPages(pages.slice(0, -1)); setSelected(null); }}>Previous resource page</Button><Button variant="outline" disabled={!list.data.continue_token} onClick={() => { setPages([...pages, token]); setToken(list.data?.continue_token ?? null); setSelected(null); }}>Next resource page</Button></div>{list.data.truncated && <p className="text-sm">The API returned more than 100 resources in a page; narrow the namespace filter.</p>}
    </>}
    {selected && <ResourceDetail key={`${selected.namespace}:${selected.name}:${selected.uid}:${canReveal}`} base={base} clusterId={id} definition={definition} version={version} item={selected} namespaces={names} close={() => setSelected(null)} done={complete} />}
  </div>;
}

function DefinitionExplorer({ id, definition, done }: { id: string; definition: Definition; done: (result: DefinitionResult) => void }) {
  const [chosen, setChosen] = useState("");
  const canReveal = useCan("secrets.reveal"); const manage = useCan("organization.manage");
  const version = chosen || definition.versions.find((v) => v.served && v.storage)?.name || definition.versions.find((v) => v.served)?.name || definition.versions[0]?.name || "";
  const schema = useQuery({ queryKey: ["custom-resource-schema", id, definition.name, definition.uid, version], queryFn: () => api.get<Schema>(`/clusters/${id}/custom-resources/definitions/${definition.name}`, { version }), enabled: Boolean(version) });
  const available = Boolean(definition.versions.find((v) => v.name === version)?.served && definition.conditions.some((c) => c.type === "Established" && c.status === "True") && !definition.conditions.some((c) => c.type === "NamesAccepted" && c.status === "False") && !definition.deletion_timestamp);
  return <div className="space-y-4"><section className="space-y-3 rounded-lg border p-4"><h2 className="font-semibold">{definition.kind} · {definition.name}</h2><p className="text-sm text-muted-foreground">{definition.group} · {definition.scope} · UID {definition.uid}</p><ConditionList conditions={definition.conditions} /><SearchableChoice label="API version" value={version} options={definition.versions.map((v) => ({ value: v.name, label: `${v.name}${v.storage ? " · storage" : ""}${v.served ? " · served" : " · unserved"}` }))} change={setChosen} /></section>
    <DefinitionManagement clusterId={id} key={`${definition.uid}:${canReveal}:${manage}`} base={`/clusters/${id}/custom-resources`} definition={definition} done={done} />
    {schema.isPending && <p>Loading CRD schema…</p>}{schema.isError && <ErrorState error={schema.error} onRetry={() => schema.refetch()} />}{schema.data && <><SchemaGuide key={`${version}:${canReveal}`} base={`/clusters/${id}/custom-resources`} definition={definition} version={version} schema={schema.data} /><Instances key={version} id={id} definition={definition} version={version} schema={schema.data} available={available} /></>}
  </div>;
}

function ClusterCustomResources({ id }: { id: string }) {
  const [selected, setSelected] = useState("");
  const client = useQueryClient();
  const [result, setResult] = useState<DefinitionResult | null>(null);
  const done = (value: DefinitionResult) => { setResult(value); setSelected(value.action === "remove" ? "" : value.name); void client.invalidateQueries({ queryKey: ["custom-resource-definitions", id] }); void client.invalidateQueries({ queryKey: ["custom-resource-schema", id] }); };
  const definitions = useQuery({ queryKey: ["custom-resource-definitions", id], queryFn: () => api.get<{ available: boolean; reason?: string; items: Definition[]; truncated: boolean }>(`/clusters/${id}/custom-resources/definitions`), refetchInterval: 30000 });
  const current = definitions.data?.items.find((item) => item.name === selected) ?? (!selected ? definitions.data?.items[0] : undefined);
  return <div className="space-y-4"><div className="flex flex-wrap items-end gap-3"><div className="min-w-64 flex-1"><SearchableChoice label="Custom resource definition" value={current?.name ?? selected} options={definitions.data?.items.map((item) => ({ value: item.name, label: `${item.kind} · ${item.name}` })) ?? []} change={setSelected} /></div><Button variant="outline" onClick={() => definitions.refetch()}>Refresh definitions</Button></div>
    {definitions.data?.available && <DefinitionInstall base={`/clusters/${id}/custom-resources`} done={done} />}
    {result && <p role="status" className="rounded border p-3 text-sm">Definition {result.status.replaceAll("_", " ")} · {result.name}. Kubernetes accepted the request; establishment and cleanup are reported separately.</p>}
    {definitions.isPending && <p>Loading custom resource definitions…</p>}{definitions.isError && <ErrorState error={definitions.error} onRetry={() => definitions.refetch()} />}
    {definitions.data && !definitions.data.available && <EmptyState title={definitions.data.reason === "forbidden" ? "CRD inventory forbidden" : "CRD API unavailable"} description={definitions.data.reason === "forbidden" ? "The registered cluster credentials cannot list CRD definitions." : "This cluster does not serve the expected CRD API."} />}
    {definitions.data?.available && !definitions.data.items.length && <EmptyState title="No CRDs installed" description="No custom resource definitions were returned by the cluster." />}
    {definitions.data?.truncated && <p className="text-sm">Definition inventory is limited to 1,000 entries.</p>}
    {current && <DefinitionExplorer key={`${current.name}:${current.uid}`} id={id} definition={current} done={done} />}
  </div>;
}

export function CustomResourcesPage() {
  return <div className="space-y-5"><PageHeader eyebrow="Configuration" title="Custom resources" description="Discover installed CRDs, inspect schemas and conditions, and manage custom resource instances." /><ClusterScope>{(id) => <ClusterCustomResources key={id} id={id} />}</ClusterScope></div>;
}
