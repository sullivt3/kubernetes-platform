"use client";

import { ArrowLeft, Plus, X } from "lucide-react";
import Link from "next/link";
import { useMemo, useState } from "react";

import { PageHeader, Section } from "@/components/common/page";
import { YamlEditor } from "@/components/common/yaml-editor";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { ClusterScope } from "@/features/kubernetes/shared";
import { useClusterNamespaces } from "@/lib/api/kube";
import { cn } from "@/lib/utils";
import { buildManifest, QTY, validateDeployment, type DeploymentInput, type KV, type Probe, type VolumeRow } from "./deployment-manifest";
import { YamlApplyPanel } from "./yaml-apply";

function KVEditor({ items, onChange, placeholder, add }: { items: KV[]; onChange: (v: KV[]) => void; placeholder: [string, string]; add: string }) {
  return (
    <div className="space-y-2">
      {items.map((item, i) => (
        <div key={i} className="flex gap-2">
          <Input placeholder={placeholder[0]} value={item.key} onChange={(e) => onChange(items.map((x, j) => (j === i ? { ...x, key: e.target.value } : x)))} className="font-mono text-xs" />
          <Input placeholder={placeholder[1]} value={item.value} onChange={(e) => onChange(items.map((x, j) => (j === i ? { ...x, value: e.target.value } : x)))} className="font-mono text-xs" />
          <Button variant="ghost" size="icon" aria-label="Remove" onClick={() => onChange(items.filter((_, j) => j !== i))}><X /></Button>
        </div>
      ))}
      <Button variant="ghost" size="xs" onClick={() => onChange([...items, { key: "", value: "" }])}><Plus /> {add}</Button>
    </div>
  );
}

function ProbeEditor({ label, value, onChange }: { label: string; value: DeploymentInput["readiness"]; onChange: (v: DeploymentInput["readiness"]) => void }) {
  return (
    <div className="space-y-1.5">
      <Label className="text-xs text-muted-foreground">{label}</Label>
      <div className="flex gap-2">
        <Select value={value.type} onValueChange={(t) => onChange({ ...value, type: t as Probe })}>
          <SelectTrigger className="w-28"><SelectValue /></SelectTrigger>
          <SelectContent><SelectItem value="none">None</SelectItem><SelectItem value="http">HTTP</SelectItem><SelectItem value="tcp">TCP</SelectItem></SelectContent>
        </Select>
        {value.type === "http" && <Input value={value.path} onChange={(e) => onChange({ ...value, path: e.target.value })} placeholder="/healthz" className="font-mono text-xs" />}
        {value.type !== "none" && <Input type="number" value={value.port} onChange={(e) => onChange({ ...value, port: Number(e.target.value) })} className="w-24" aria-label="Port" />}
      </div>
    </div>
  );
}

function Form({ clusterId }: { clusterId: string }) {
  const namespaces = useClusterNamespaces(clusterId);
  const [f, setF] = useState<DeploymentInput>({
    name: "", namespace: "default", image: "", replicas: 2, ports: [80], env: [],
    requests: { cpu: "100m", memory: "128Mi" }, limits: { cpu: "", memory: "256Mi" },
    readiness: { type: "http", path: "/", port: 80 }, liveness: { type: "none", path: "/", port: 80 },
    volumes: [], pullSecrets: "", nodeSelector: [], spreadAcrossNodes: true, service: "ClusterIP",
  });
  const [review, setReview] = useState<string | null>(null);
  const set = (patch: Partial<DeploymentInput>) => setF((prev) => ({ ...prev, ...patch }));
  const errors = validateDeployment(f);
  const manifest = useMemo(() => buildManifest(f), [f]);

  if (review !== null) {
    return (
      <Section title="Review and apply" description="Edit the generated YAML if needed, run a dry run to validate against the cluster, then apply.">
        <Button variant="ghost" size="sm" className="-ml-2" onClick={() => setReview(null)}><ArrowLeft /> Back to the form</Button>
        <YamlApplyPanel clusterId={clusterId} initial={review} defaultNamespace={f.namespace} allowNamespace={false} />
      </Section>
    );
  }

  return (
    <div className="grid gap-6 xl:grid-cols-[1fr_28rem]">
      <Card>
        <CardContent className="space-y-6">
          <div className="grid gap-4 md:grid-cols-3">
            <div className="space-y-1.5"><Label>Name</Label><Input value={f.name} onChange={(e) => set({ name: e.target.value })} placeholder="web" /></div>
            <div className="space-y-1.5">
              <Label>Namespace</Label>
              <Select value={f.namespace} onValueChange={(v) => set({ namespace: v })}>
                <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
                <SelectContent>{(namespaces.data ?? [{ name: "default" }]).map((n) => <SelectItem key={n.name} value={n.name}>{n.name}</SelectItem>)}</SelectContent>
              </Select>
            </div>
            <div className="space-y-1.5"><Label>Replicas</Label><Input type="number" min={0} value={f.replicas} onChange={(e) => set({ replicas: Number(e.target.value) })} /></div>
          </div>
          <div className="grid gap-4 md:grid-cols-[1fr_14rem]">
            <div className="space-y-1.5"><Label>Container image</Label><Input value={f.image} onChange={(e) => set({ image: e.target.value })} placeholder="registry.example.com/team/app:1.4.2" className="font-mono text-xs" /></div>
            <div className="space-y-1.5">
              <Label>Container ports</Label>
              <Input value={f.ports.join(", ")} onChange={(e) => set({ ports: e.target.value.split(",").map((p) => p.trim()).filter(Boolean).map(Number) })} placeholder="80, 9090" className="font-mono text-xs" />
            </div>
          </div>
          <div className="space-y-1.5"><Label>Environment variables</Label><KVEditor items={f.env} onChange={(env) => set({ env })} placeholder={["NAME", "value"]} add="Add variable" /></div>
          <div className="grid gap-4 md:grid-cols-4">
            {(["requests", "limits"] as const).map((group) =>
              (["cpu", "memory"] as const).map((res) => (
                <div key={`${group}-${res}`} className="space-y-1.5">
                  <Label className="text-xs text-muted-foreground">{res.toUpperCase()} {group === "requests" ? "request" : "limit"}</Label>
                  <Input value={f[group][res]} onChange={(e) => set({ [group]: { ...f[group], [res]: e.target.value } } as Partial<DeploymentInput>)} placeholder={res === "cpu" ? "250m" : "256Mi"} className={cn("font-mono text-xs", !QTY.test(f[group][res]) && "border-red-500/60")} />
                </div>
              )),
            )}
          </div>
          <div className="grid gap-4 md:grid-cols-2">
            <ProbeEditor label="Readiness check" value={f.readiness} onChange={(readiness) => set({ readiness })} />
            <ProbeEditor label="Liveness check" value={f.liveness} onChange={(liveness) => set({ liveness })} />
          </div>
          <div className="space-y-2">
            <Label>Volumes</Label>
            {f.volumes.map((v, i) => (
              <div key={i} className="flex flex-wrap gap-2">
                <Input value={v.name} onChange={(e) => set({ volumes: f.volumes.map((x, j) => (j === i ? { ...x, name: e.target.value } : x)) })} placeholder="name" className="w-32" />
                <Select value={v.type} onValueChange={(t) => set({ volumes: f.volumes.map((x, j) => (j === i ? { ...x, type: t as VolumeRow["type"] } : x)) })}>
                  <SelectTrigger className="w-44"><SelectValue /></SelectTrigger>
                  <SelectContent><SelectItem value="emptyDir">emptyDir</SelectItem><SelectItem value="configMap">ConfigMap</SelectItem><SelectItem value="secret">Secret</SelectItem><SelectItem value="persistentVolumeClaim">PVC</SelectItem></SelectContent>
                </Select>
                {v.type !== "emptyDir" && <Input value={v.source} onChange={(e) => set({ volumes: f.volumes.map((x, j) => (j === i ? { ...x, source: e.target.value } : x)) })} placeholder="source name" className="w-40" />}
                <Input value={v.mountPath} onChange={(e) => set({ volumes: f.volumes.map((x, j) => (j === i ? { ...x, mountPath: e.target.value } : x)) })} placeholder="/data" className="w-40 font-mono text-xs" />
                <Button variant="ghost" size="icon" aria-label="Remove volume" onClick={() => set({ volumes: f.volumes.filter((_, j) => j !== i) })}><X /></Button>
              </div>
            ))}
            <Button variant="ghost" size="xs" onClick={() => set({ volumes: [...f.volumes, { name: "", type: "emptyDir", source: "", mountPath: "" }] })}><Plus /> Add volume</Button>
          </div>
          <div className="grid gap-4 md:grid-cols-2">
            <div className="space-y-1.5"><Label>Image pull secrets</Label><Input value={f.pullSecrets} onChange={(e) => set({ pullSecrets: e.target.value })} placeholder="regcred, other-secret" className="font-mono text-xs" /></div>
            <div className="space-y-1.5">
              <Label>Expose with a Service</Label>
              <Select value={f.service} onValueChange={(v) => set({ service: v as DeploymentInput["service"] })}>
                <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
                <SelectContent><SelectItem value="none">No Service</SelectItem><SelectItem value="ClusterIP">ClusterIP (in-cluster)</SelectItem><SelectItem value="NodePort">NodePort</SelectItem><SelectItem value="LoadBalancer">LoadBalancer</SelectItem></SelectContent>
              </Select>
            </div>
          </div>
          <div className="space-y-2">
            <Label>Scheduling</Label>
            <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={f.spreadAcrossNodes} onChange={(e) => set({ spreadAcrossNodes: e.target.checked })} /> Spread replicas across nodes (soft constraint)</label>
            <KVEditor items={f.nodeSelector} onChange={(nodeSelector) => set({ nodeSelector })} placeholder={["node label key", "value"]} add="Require node label" />
          </div>
          {errors.length > 0 && <ul className="space-y-1 text-sm text-amber-300/90">{errors.map((e) => <li key={e}>• {e}</li>)}</ul>}
          <div className="flex justify-end border-t border-border/60 pt-4">
            <Button disabled={errors.length > 0} onClick={() => setReview(manifest)}>Review YAML</Button>
          </div>
        </CardContent>
      </Card>
      <div className="space-y-2">
        <p className="text-sm font-medium">Generated manifest</p>
        <YamlEditor value={manifest} readOnly height="720px" />
      </div>
    </div>
  );
}

export function NewDeploymentPage() {
  return (
    <div className="space-y-6">
      <Button asChild variant="ghost" size="sm" className="-ml-2"><Link href="/workloads/deployments"><ArrowLeft /> Deployments</Link></Button>
      <PageHeader eyebrow="Workloads" title="New deployment" description="Describe the application; the platform generates the Kubernetes manifests, validates them with a server dry run and applies them." />
      <ClusterScope>{(id) => <Form clusterId={id} />}</ClusterScope>
    </div>
  );
}
