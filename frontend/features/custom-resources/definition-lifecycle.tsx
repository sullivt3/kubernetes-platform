"use client";

import { useEffect, useRef, useState } from "react";
import { parse } from "yaml";
import { ConfirmDialog } from "@/components/common/confirm-dialog";
import { ErrorState } from "@/components/common/page";
import { YamlEditor } from "@/components/common/yaml-editor";
import { useCan } from "@/components/providers/auth";
import { Button } from "@/components/ui/button";
import { api, ApiError } from "@/lib/api/client";

type DefinitionIdentity = { name: string; uid: string; managed?: boolean; deletion_timestamp?: string | null };
export type DefinitionResult = { name: string; uid: string; action: string; status: string };
type Revealed = { yaml: string; edit_yaml: string; content_hash: string; uid: string };
type Review = { id: string; name: string; action: string; expires_at: string; fields: string[]; requires_empty: boolean; notice: string };
type Action = "install" | "update" | "remove";

const TEMPLATE = `apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: widgets.example.com
spec:
  group: example.com
  scope: Namespaced
  names:
    plural: widgets
    singular: widget
    kind: Widget
  versions:
    - name: v1
      served: true
      storage: true
      schema:
        openAPIV3Schema:
          type: object
          properties:
            spec:
              type: object
              properties:
                message:
                  type: string
`;

function DefinitionForm({ base, action, definition, revealed, close, done }: {
  base: string; action: Action; definition?: DefinitionIdentity; revealed?: Revealed;
  close: () => void; done: (result: DefinitionResult) => void;
}) {
  const [text, setText] = useState(revealed?.edit_yaml ?? TEMPLATE);
  const [review, setReview] = useState<Review | null>(null);
  const [busy, setBusy] = useState(false);
  const [executing, setExecuting] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const epoch = useRef(0);
  useEffect(() => () => { epoch.current += 1; }, []);
  let name = definition?.name ?? "";
  if (action === "install") { try { const doc = parse(text); name = typeof doc?.metadata?.name === "string" ? doc.metadata.name : ""; } catch { name = ""; } }
  const preview = async () => {
    const request = ++epoch.current; setBusy(true); setError(null); setReview(null);
    try {
      const value = await api.post<Review>(base + "/definition-reviews", { name, action, expected_uid: definition?.uid, expected_hash: revealed?.content_hash, manifest: action === "remove" ? "" : text });
      if (request === epoch.current) setReview(value);
    } catch (e) { if (request === epoch.current) setError(e); }
    finally { if (request === epoch.current) setBusy(false); }
  };
  const execute = async () => {
    if (!review) return;
    const request = epoch.current; setExecuting(true); setError(null);
    try {
      const value = await api.post<DefinitionResult>(base + "/definition-execute", { review_id: review.id, confirm: review.name });
      if (request === epoch.current) done(value);
    } catch (e) { if (request === epoch.current) setError(e); }
    finally { if (request === epoch.current) setExecuting(false); }
  };
  const fields = error instanceof ApiError && Array.isArray(error.details.fields) ? error.details.fields.filter((item): item is string => typeof item === "string") : [];
  return <section className="space-y-3 rounded-lg border p-4" aria-label="Definition action form">
    <div className="flex items-center justify-between"><h3 className="font-semibold capitalize">{action} CRD definition</h3><Button variant="ghost" disabled={executing} onClick={close}>Close definition form</Button></div>
    <p className="text-sm text-muted-foreground">Definition lifecycle actions require organization administrator access. Kubernetes validates the submitted definition. Server acceptance is separate from API establishment.</p>
    {action === "remove" ? <p className="text-sm">Removal uninstalls this API. Instance inventory must be empty at review and execution. Pause controllers and other writers first: Kubernetes can also delete instances created after the final inventory check. Finalizers are preserved.</p> : <>
      <p className="text-sm text-muted-foreground">Schema/API changes require zero instances. Keep the storage version and existing served versions; storage migration and conversion webhook configuration use separate workflows. Existing definitions are never adopted.</p>
      <div aria-label="CRD definition YAML editor"><YamlEditor value={text} readOnly={executing} height="380px" onChange={(value) => { epoch.current += 1; setText(value); setReview(null); setConfirm(false); setBusy(false); setError(null); }} /></div>
    </>}
    <Button variant="outline" disabled={busy || executing || !name || (action !== "remove" && text.length > 131072)} onClick={() => void preview()}>Review definition {action}</Button>
    {action !== "remove" && text.length > 131072 && <p className="text-sm text-destructive">Definition YAML is limited to 128 KiB.</p>}
    {error !== null && <ErrorState error={error} />}{fields.length > 0 && <p className="text-sm text-destructive">Validation fields: {fields.join(", ")}</p>}
    {review && <div className="space-y-2 rounded border p-3"><h4 className="font-medium">Reviewed definition action</h4><p className="font-mono text-xs">{review.action} · {review.name}</p><p className="text-sm">{review.notice} Expires {new Date(review.expires_at).toLocaleTimeString()}.</p>{review.requires_empty && <p className="text-sm">No instances were found. Inventory will be checked again before execution.</p>}<ul className="max-h-40 overflow-auto font-mono text-xs">{review.fields.map((field) => <li key={field}>{field}</li>)}</ul><Button disabled={busy || executing} onClick={() => setConfirm(true)}>Confirm definition {action}</Button></div>}
    <ConfirmDialog key={`${confirm}:${review?.id ?? "none"}`} open={confirm} onOpenChange={setConfirm} title={`Confirm definition ${action}`} description={action === "remove" ? "Uninstall this API definition. Kubernetes can delete all its instances, including objects created concurrently after the empty inventory check. Pause all controllers and writers before proceeding. This action preserves finalizers." : "Send this reviewed definition to Kubernetes. Definition identity, ownership, content and storage versions will be checked again. API establishment is reported separately."} confirmLabel={`Execute definition ${action}`} confirmText={review?.name} destructive={action !== "install"} pending={executing} onConfirm={() => void execute()} />
  </section>;
}

export function DefinitionInstall({ base, done }: { base: string; done: (result: DefinitionResult) => void }) {
  const manage = useCan("organization.manage"); const write = useCan("clusters.write");
  const [open, setOpen] = useState(false);
  if (!manage || !write) return null;
  return open ? <DefinitionForm base={base} action="install" close={() => setOpen(false)} done={(value) => { setOpen(false); done(value); }} /> : <Button variant="outline" onClick={() => setOpen(true)}>Install CRD definition</Button>;
}

export function DefinitionManagement({ base, definition, done }: { base: string; definition: DefinitionIdentity; done: (result: DefinitionResult) => void }) {
  const manage = useCan("organization.manage"); const write = useCan("clusters.write"); const canReveal = useCan("secrets.reveal");
  const [revealed, setRevealed] = useState<Revealed | null>(null);
  const [form, setForm] = useState<"update" | "remove" | null>(null);
  const [busy, setBusy] = useState(false); const [error, setError] = useState<unknown>(null);
  const epoch = useRef(0); useEffect(() => () => { epoch.current += 1; }, []);
  const reveal = async () => {
    const request = ++epoch.current; setBusy(true); setError(null);
    try { const value = await api.post<Revealed>(base + `/definitions/${definition.name}/manifest/reveal`, { name: definition.name, expected_uid: definition.uid }); if (request === epoch.current) setRevealed(value); }
    catch (e) { if (request === epoch.current) setError(e); }
    finally { if (request === epoch.current) setBusy(false); }
  };
  const mutable = manage && write && definition.managed && !definition.deletion_timestamp;
  return <section className="space-y-3 rounded-lg border p-4" aria-label="Definition management"><h3 className="font-semibold">Definition management</h3>
    <p className="text-sm text-muted-foreground">{definition.managed ? "Installed through this platform. Administrators can review updates and removal." : "Externally managed definition. Use its Helm/operator installation workflow for lifecycle changes; this page does not adopt it."}</p>
    {definition.deletion_timestamp && <p role="status" className="text-sm">Definition removal pending. Kubernetes cleanup and finalizers may still be running.</p>}
    <div className="flex flex-wrap gap-2">{canReveal && !revealed && <Button variant="outline" disabled={busy} onClick={() => void reveal()}>Reveal definition YAML</Button>}{canReveal && revealed && <Button variant="outline" onClick={() => { epoch.current += 1; setRevealed(null); setForm(null); setBusy(false); }}>Hide definition YAML</Button>}{mutable && canReveal && revealed && <Button variant="outline" onClick={() => setForm("update")}>Edit CRD definition</Button>}{mutable && <Button variant="outline" onClick={() => setForm("remove")}>Remove CRD definition</Button>}</div>
    {canReveal && revealed && !form && <YamlEditor value={revealed.yaml} readOnly height="380px" />}
    {mutable && form && (form === "remove" || canReveal) && <DefinitionForm key={form} base={base} action={form} definition={definition} revealed={form === "update" ? revealed ?? undefined : undefined} close={() => setForm(null)} done={(value) => { setRevealed(null); setForm(null); done(value); }} />}
    {error !== null && <ErrorState error={error} />}
  </section>;
}
