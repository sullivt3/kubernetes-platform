"use client";

import { CheckCircle2, FileUp, Info, ShieldAlert, Upload } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { toast } from "sonner";

import { PageHeader } from "@/components/common/page";
import { StatusPill } from "@/components/common/status";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { ApiError } from "@/lib/api/client";
import { useImportCluster, useInspectKubeconfig, type KubeconfigContext } from "@/lib/api/kube";
import { cn } from "@/lib/utils";

const NAME = /^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$/;

function suggestName(context: string): string {
  return context.toLowerCase().replace(/[^a-z0-9-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 63) || "cluster";
}

export function ImportClusterPage() {
  const router = useRouter();
  const inspect = useInspectKubeconfig();
  const importCluster = useImportCluster();
  const [kubeconfig, setKubeconfig] = useState("");
  const [contexts, setContexts] = useState<KubeconfigContext[] | null>(null);
  const [context, setContext] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [override, setOverride] = useState("");
  const [tlsName, setTlsName] = useState("");
  const [error, setError] = useState<string | null>(null);

  const chosen = contexts?.find((c) => c.name === context);
  const nameError = name && !NAME.test(name) ? "Lowercase letters, digits and '-'" : null;

  const runInspect = (text: string) => {
    setError(null);
    setContexts(null);
    inspect.mutate(text, {
      onSuccess: (result) => {
        setContexts(result.contexts);
        const preferred =
          result.contexts.find((c) => c.name === result.current_context && c.supported) ?? result.contexts.find((c) => c.supported);
        setContext(preferred?.name ?? null);
        if (preferred && !name) setName(suggestName(preferred.cluster || preferred.name));
      },
      onError: (e) => setError(e instanceof ApiError ? e.message : String(e)),
    });
  };

  const submit = () => {
    setError(null);
    importCluster.mutate(
      {
        name,
        kubeconfig,
        context,
        description: description || null,
        server_override: override.trim() || null,
        tls_server_name: tlsName.trim() || null,
      },
      {
        onSuccess: ({ operation }) => {
          toast.success(`Importing ${name}`, { description: "Verifying the connection to the cluster…" });
          router.push(`/operations/${operation.id}`);
        },
        onError: (e) => setError(e instanceof ApiError ? e.message : String(e)),
      },
    );
  };

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow="Kubernetes"
        title="Import cluster"
        description="Connect an existing cluster with a kubeconfig. Only the context you choose is stored, encrypted; it is never sent back to the browser."
      />
      <div className="grid gap-6 lg:grid-cols-[1fr_20rem]">
        <Card>
          <CardContent className="space-y-6">
            <section className="space-y-2">
              <div className="flex items-center justify-between">
                <Label htmlFor="kubeconfig">1. Kubeconfig</Label>
                <label className="inline-flex cursor-pointer items-center gap-1.5 text-xs text-sky-400 hover:underline">
                  <FileUp className="size-3.5" /> Load from file
                  <input
                    type="file"
                    className="sr-only"
                    onChange={async (e) => {
                      const file = e.target.files?.[0];
                      if (!file) return;
                      const text = await file.text();
                      setKubeconfig(text);
                      runInspect(text);
                    }}
                  />
                </label>
              </div>
              <Textarea
                id="kubeconfig"
                value={kubeconfig}
                onChange={(e) => {
                  setKubeconfig(e.target.value);
                  setContexts(null);
                }}
                placeholder="apiVersion: v1&#10;kind: Config&#10;clusters: …"
                className="min-h-48 font-mono text-xs"
                spellCheck={false}
                autoComplete="off"
              />
              <Button size="sm" variant="outline" disabled={!kubeconfig.trim() || inspect.isPending} onClick={() => runInspect(kubeconfig)}>
                {inspect.isPending ? "Reading…" : "Read contexts"}
              </Button>
            </section>

            {contexts && (
              <section className="space-y-2">
                <Label>2. Context</Label>
                <ul className="divide-y divide-border/60 rounded-md border border-border">
                  {contexts.map((c) => (
                    <li key={c.name}>
                      <label className={cn("flex items-start gap-3 px-3 py-2.5", c.supported ? "cursor-pointer hover:bg-muted/30" : "opacity-60")}>
                        <input
                          type="radio"
                          name="context"
                          className="mt-1"
                          disabled={!c.supported}
                          checked={context === c.name}
                          onChange={() => {
                            setContext(c.name);
                            if (!name) setName(suggestName(c.cluster || c.name));
                          }}
                        />
                        <div className="min-w-0 flex-1 text-sm">
                          <p className="font-medium">{c.name}</p>
                          <p className="truncate font-mono text-xs text-muted-foreground">{c.server ?? "no server"}</p>
                          {c.problem && <p className="mt-1 text-xs text-amber-300">{c.problem}</p>}
                        </div>
                        <StatusPill tone={c.supported ? "neutral" : "warning"}>{c.auth_type}</StatusPill>
                      </label>
                    </li>
                  ))}
                </ul>
              </section>
            )}

            {chosen && (
              <section className="space-y-4">
                <Label>3. Details</Label>
                <div className="grid gap-4 md:grid-cols-2">
                  <div className="space-y-1.5">
                    <Label htmlFor="name" className="text-xs text-muted-foreground">Name in the platform</Label>
                    <Input id="name" value={name} onChange={(e) => setName(e.target.value)} aria-invalid={Boolean(nameError)} />
                    {nameError && <p className="text-xs text-red-400">{nameError}</p>}
                  </div>
                  <div className="space-y-1.5">
                    <Label htmlFor="desc" className="text-xs text-muted-foreground">Description (optional)</Label>
                    <Input id="desc" value={description} onChange={(e) => setDescription(e.target.value)} />
                  </div>
                </div>
                <details className="rounded-md border border-border p-3 text-sm">
                  <summary className="cursor-pointer font-medium">Connection (optional)</summary>
                  <p className="mt-2 text-xs text-muted-foreground">
                    Only needed when the platform cannot reach <span className="font-mono">{chosen.server}</span> directly — for
                    example through NAT, a proxy or a load balancer. The API server certificate is still verified.
                  </p>
                  <div className="mt-3 grid gap-4 md:grid-cols-2">
                    <Input value={override} onChange={(e) => setOverride(e.target.value)} placeholder="API server override, e.g. https://lb:6443" className="font-mono text-xs" />
                    <Input value={tlsName} onChange={(e) => setTlsName(e.target.value)} placeholder="TLS server name (name/IP in the certificate)" className="font-mono text-xs" />
                  </div>
                </details>
              </section>
            )}

            {error && <p role="alert" className="rounded border border-red-500/30 bg-red-500/5 p-2 text-sm text-red-300">{error}</p>}

            <div className="flex justify-end border-t border-border/60 pt-4">
              <Button disabled={!chosen || !name || Boolean(nameError) || importCluster.isPending} onClick={submit}>
                <Upload /> {importCluster.isPending ? "Importing…" : "Import and connect"}
              </Button>
            </div>
          </CardContent>
        </Card>
        <div className="space-y-4 text-sm">
          <div className="flex gap-3 rounded-lg border border-sky-500/20 bg-sky-500/5 p-4">
            <Info className="mt-0.5 size-4 shrink-0 text-sky-400" />
            <div className="space-y-2 text-muted-foreground">
              <p className="font-medium text-foreground">Supported credentials</p>
              <p className="flex gap-1.5"><CheckCircle2 className="mt-0.5 size-3.5 shrink-0 text-emerald-400" /> Embedded client certificates</p>
              <p className="flex gap-1.5"><CheckCircle2 className="mt-0.5 size-3.5 shrink-0 text-emerald-400" /> Bearer tokens (e.g. a service account)</p>
              <p>
                Export a self-contained kubeconfig with{" "}
                <span className="font-mono text-xs text-foreground">kubectl config view --raw --minify --flatten</span>.
              </p>
            </div>
          </div>
          <div className="flex gap-3 rounded-lg border border-amber-500/20 bg-amber-500/5 p-4">
            <ShieldAlert className="mt-0.5 size-4 shrink-0 text-amber-400" />
            <p className="text-muted-foreground">
              Kubeconfigs that run authentication plugins (common for EKS, GKE and AKS) are not accepted: the platform never
              executes external programs. Create a dedicated service-account token for the platform instead.
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}
