"use client";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { parse } from "yaml";
import { ConfirmDialog } from "@/components/common/confirm-dialog";
import { ErrorState } from "@/components/common/page";
import { SearchableChoice } from "@/components/common/searchable-choice";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { api } from "@/lib/api/client";
import { useClusterNamespaces } from "@/lib/api/kube";

type Snapshot = { edit_yaml: string; content_hash: string };
type Reviewed = { id: string; name: string; expires_at: string; notice: string; reviewed_instances: number; backend?: string; mode?: string; source_version?: string; target_version?: string; strategy?: string };
type History = { id: string; category: string; operation_id?: string | null; created_at: string; summary: { reviewed_instances: number }; progress: Record<string, unknown> };

export function AdvancedCrdOperations({ clusterId, base, name, uid, snapshot }: { clusterId: string; base: string; name: string; uid: string; snapshot: Snapshot }) {
  const [mode, setMode] = useState<"storage_migration" | "conversion_webhook">("storage_migration");
  const [storageMode, setStorageMode] = useState("certified");
  const [version, setVersion] = useState(""); const [strategy, setStrategy] = useState("Webhook"); const [namespace, setNamespace] = useState("default"); const [service, setService] = useState(""); const [port, setPort] = useState("443"); const [path, setPath] = useState("/convert"); const [ca, setCa] = useState("");
  const [review, setReview] = useState<Reviewed | null>(null); const [busy, setBusy] = useState(false); const [error, setError] = useState<unknown>(null); const [confirm, setConfirm] = useState(false); const [operation, setOperation] = useState<string | null>(null);
  const epoch = useRef(0); useEffect(() => () => { epoch.current += 1; }, []);
  const namespaces = useClusterNamespaces(clusterId);
  const services = useQuery({ queryKey: ["conversion-services", clusterId, namespace], queryFn: () => api.get<{ items: { name: string }[]; truncated: boolean }>(`/clusters/${clusterId}/resources/services`, { namespace }), enabled: mode === "conversion_webhook" && strategy === "Webhook" });
  const history = useQuery({ queryKey: ["definition-operations", clusterId, name], queryFn: () => api.get<History[]>(base + "/definition-operations", { name }), refetchInterval: 5000 });
  let versions: { name: string; served: boolean; storage: boolean }[] = [];
  try { versions = parse(snapshot.edit_yaml)?.spec?.versions ?? []; } catch { /* The API validates revealed YAML. */ }
  const selectedVersion = version || versions.find((v) => v.served && !v.storage)?.name || versions.find((v) => v.storage)?.name || "";
  const reset = () => { epoch.current += 1; setReview(null); setConfirm(false); setBusy(false); setError(null); };
  const preview = async (probe = false) => {
    const request = ++epoch.current; setBusy(true); setError(null); setReview(null);
    try { const target = { name, expected_uid: uid, expected_hash: snapshot.content_hash }; const value = await api.post<Reviewed>(base + (mode === "storage_migration" ? "/migration-reviews" : "/conversion-reviews"), mode === "storage_migration" ? { ...target, target_version: selectedVersion, mode: storageMode } : { ...target, action: probe ? "probe" : "configure", strategy, service_namespace: namespace, service_name: service, port: Number(port), path, ca_pem: ca }); if (request === epoch.current) setReview(value); }
    catch (e) { if (request === epoch.current) setError(e); } finally { if (request === epoch.current) setBusy(false); }
  };
  const execute = async () => {
    const request = epoch.current; setBusy(true); setError(null);
    try { const value = await api.post<{ id: string }>(base + "/automation-run", { category: mode, review_id: review?.id, confirm: name }); if (request === epoch.current) { setOperation(value.id); setConfirm(false); setReview(null); void history.refetch(); } }
    catch (e) { if (request === epoch.current) setError(e); } finally { if (request === epoch.current) setBusy(false); }
  };
  return <details className="space-y-3 rounded border p-4"><summary className="cursor-pointer font-medium">Storage migration & conversion webhooks</summary>
    <p className="text-sm text-muted-foreground">These administrator operations use tracked progress, checkpoints and cancellation. Back up resources and pause external writers to the definition before proceeding. Reveal current definition YAML again after any definition change.</p>
    <SearchableChoice label="CRD operation" value={mode} options={[{ value: "storage_migration", label: "Storage migration" }, { value: "conversion_webhook", label: "Conversion webhook" }]} change={(v) => { setMode(v as typeof mode); reset(); }} />
    {mode === "storage_migration" ? <><SearchableChoice label="Storage action" value={storageMode} options={[{ value: "certified", label: "Migrate and certify (native controller)" }, { value: "rewrite_only", label: "Rewrite current storage only (uncertified)" }]} change={(v) => { setStorageMode(v); if (v === "rewrite_only") setVersion(versions.find((item) => item.storage)?.name ?? ""); reset(); }} /><SearchableChoice label="Target storage version" value={selectedVersion} options={versions.filter((v) => v.served).map((v) => ({ value: v.name, label: `${v.name}${v.storage ? " · current storage" : ""}` }))} change={(v) => { setVersion(v); reset(); }} /><p className="text-sm">Migration selects this served version for storage, rewrites current instances with identity/concurrency checks, then updates stored-version metadata only after a complete pass. Partial failures retain old stored-version metadata and the selected storage version; resume through Operations. None conversion requires identical schemas. Storage migration certification requires the native Kubernetes migration API/controller. Optional rewrite-only mode requires one observable control plane, uses the current storage version and retains old stored-version history. It never claims migration certification. Limit: 5,000 instances per pass.</p></> : <>
      <SearchableChoice label="Conversion strategy" value={strategy} options={[{ value: "Webhook", label: "Webhook" }, { value: "None", label: "None" }]} change={(v) => { setStrategy(v); reset(); }} />
      {strategy === "Webhook" && <><SearchableChoice label="Webhook namespace" value={namespace} options={namespaces.data?.map((n) => ({ value: n.name, label: n.name })) ?? []} change={(v) => { setNamespace(v); setService(""); reset(); }} /><SearchableChoice label="Webhook Service" value={service} options={services.data?.items.map((s) => ({ value: s.name, label: s.name })) ?? []} change={(v) => { setService(v); reset(); }} /><Input aria-label="Webhook Service port" value={port} onChange={(e) => { setPort(e.target.value); reset(); }} /><Input aria-label="Webhook conversion path" value={path} onChange={(e) => { setPath(e.target.value); reset(); }} /><Textarea aria-label="Webhook CA PEM bundle" placeholder="PEM CA bundle" value={ca} onChange={(e) => { setCa(e.target.value); reset(); }} />{services.isError && <ErrorState error={services.error} />}{namespaces.isError && <ErrorState error={namespaces.error} />}{services.data?.truncated && <p className="text-sm">Service inventory is truncated. Narrow the namespace selection.</p>}</>}
      <p className="text-sm">Configure an existing TLS conversion server through a Service with Ready EndpointSlice backends. This does not install the server. Kubernetes performs ConversionReview v1 calls using the supplied CA. Existing instances are probed through served APIs; failed configuration probes conditionally restore the prior specification. With zero instances, service readiness alone cannot verify the conversion protocol. Probe limit: 200 instances.</p>
    </>}
    <div className="flex gap-2"><Button variant="outline" disabled={busy || (mode === "storage_migration" && !selectedVersion)} onClick={() => void preview()}>Review {mode === "storage_migration" ? "storage migration" : "webhook configuration"}</Button>{mode === "conversion_webhook" && <Button variant="outline" disabled={busy} onClick={() => void preview(true)}>Review conversion probe</Button>}</div>
    {review && <div className="space-y-2 rounded border p-3"><h4 className="font-medium">Reviewed CRD operation</h4><p className="text-sm">{review.notice} {review.backend ? `Backend: ${review.backend}. ` : ""}{review.reviewed_instances} instances inspected. {review.source_version && `${review.source_version} → ${review.target_version}`}</p><p className="text-xs">Expires {new Date(review.expires_at).toLocaleTimeString()}.</p><Button disabled={busy} onClick={() => setConfirm(true)}>Confirm CRD operation</Button></div>}
    {error !== null && <ErrorState error={error} />}{operation && <p role="status"><Link className="text-sm underline" href={`/operations/${operation}`}>View CRD operation progress</Link></p>}
    <h4 className="font-medium">CRD operation history</h4>{history.isError && <ErrorState error={history.error} />}{history.data?.map((item) => <p key={item.id} className="text-sm">{new Date(item.created_at).toLocaleString()} · {item.category.replaceAll("_", " ")} · {item.operation_id ? <Link className="underline" href={`/operations/${item.operation_id}`}>View operation</Link> : "Reviewed; not submitted"}</p>)}
    <ConfirmDialog key={`${confirm}:${review?.id}`} open={confirm} onOpenChange={setConfirm} title="Execute reviewed CRD operation" description={mode === "storage_migration" ? "Certified migration switches storage and delegates to the native controller. Rewrite-only mode never changes storage selection or certifies metadata. Cancellation requests removal of an owned native job; accepted controller writes can still complete. Keep a backup and pause external definition writers." : "Configure or test conversion through Kubernetes. A failed configuration probe attempts a resource-version-guarded restoration; concurrent changes can prevent rollback. Configuration with no instances cannot establish protocol correctness."} confirmText={name} confirmLabel="Execute CRD operation" pending={busy} onConfirm={() => void execute()} />
  </details>;
}
