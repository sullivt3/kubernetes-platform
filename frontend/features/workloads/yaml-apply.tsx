"use client";

import { CheckCircle2, ChevronRight, Download, FileUp, FlaskConical, Rocket, XCircle } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import { StatusPill } from "@/components/common/status";
import { YamlEditor, yamlProblems } from "@/components/common/yaml-editor";
import { Button } from "@/components/ui/button";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ApiError } from "@/lib/api/client";
import { useApply, type ApplyResponse, type PlanResult } from "@/lib/api/workloads";
import { cn } from "@/lib/utils";

function DiffView({ diff }: { diff: string }) {
  if (!diff) return <p className="p-3 text-xs text-muted-foreground">No changes.</p>;
  return (
    <pre className="max-h-96 overflow-auto bg-black/40 p-3 font-mono text-xs leading-relaxed">
      {diff.split("\n").map((line, i) => (
        <div
          key={i}
          className={cn(
            line.startsWith("+") && !line.startsWith("+++") && "bg-emerald-500/10 text-emerald-300",
            line.startsWith("-") && !line.startsWith("---") && "bg-red-500/10 text-red-300",
            line.startsWith("@@") && "text-sky-400",
            (line.startsWith("+++") || line.startsWith("---")) && "text-muted-foreground",
          )}
        >
          {line || " "}
        </div>
      ))}
    </pre>
  );
}

const ACTION_TONE = { create: "info", update: "warning", unchanged: "neutral", error: "danger" } as const;

function ResultRow({ result }: { result: PlanResult }) {
  const [open, setOpen] = useState(result.action === "update" || result.action === "error");
  return (
    <Collapsible open={open} onOpenChange={setOpen} className="border-b border-border/60 last:border-0">
      <CollapsibleTrigger className="group flex w-full items-center gap-3 px-3 py-2 text-left text-sm hover:bg-muted/30">
        <ChevronRight className="size-4 transition-transform group-data-[state=open]:rotate-90" />
        <StatusPill tone={ACTION_TONE[result.action]}>{result.applied ? `${result.action}d` : result.action}</StatusPill>
        <span className="font-medium">{result.kind}</span>
        <span className="font-mono text-xs text-muted-foreground">{result.namespace ? `${result.namespace}/` : ""}{result.name}</span>
        {result.applied && <CheckCircle2 className="ml-auto size-4 text-emerald-400" />}
      </CollapsibleTrigger>
      <CollapsibleContent>
        {result.note && <p className="px-3 pb-2 text-xs text-amber-300">{result.note}</p>}
        {result.error ? (
          <div className="space-y-1 px-3 pb-3 text-xs">
            <p className="text-red-300">{result.error}</p>
            {result.causes?.map((c, i) => (
              <p key={i} className="font-mono text-red-200/80">{c.field}: {c.message}</p>
            ))}
          </div>
        ) : (
          <DiffView diff={result.diff ?? ""} />
        )}
      </CollapsibleContent>
    </Collapsible>
  );
}

export function PlanResults({ response }: { response: ApplyResponse }) {
  const counts = response.results.reduce<Record<string, number>>((acc, r) => ({ ...acc, [r.action]: (acc[r.action] ?? 0) + 1 }), {});
  return (
    <div className="space-y-2">
      <p className="flex items-center gap-2 text-sm">
        {response.ok ? <CheckCircle2 className="size-4 text-emerald-400" /> : <XCircle className="size-4 text-red-400" />}
        {response.dry_run ? "Server dry run" : "Applied"}:{" "}
        {Object.entries(counts).map(([action, n]) => `${n} ${action}`).join(", ")}
      </p>
      <div className="rounded-md border border-border">{response.results.map((r, i) => <ResultRow key={i} result={r} />)}</div>
    </div>
  );
}

/**
 * Edit → server dry run (diff + validation) → apply. Apply is only enabled after a
 * successful dry run of the exact text being applied.
 */
export function YamlApplyPanel({
  clusterId,
  initial,
  defaultNamespace = "default",
  allowNamespace = true,
  allowFiles = true,
  height,
  onApplied,
}: {
  clusterId: string;
  initial: string;
  defaultNamespace?: string;
  allowNamespace?: boolean;
  allowFiles?: boolean;
  height?: string;
  onApplied?: (response: ApplyResponse) => void;
}) {
  const [text, setText] = useState(initial);
  const [namespace, setNamespace] = useState(defaultNamespace);
  const [planned, setPlanned] = useState<{ text: string; response: ApplyResponse } | null>(null);
  const [applied, setApplied] = useState<ApplyResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const apply = useApply(clusterId);
  const syntax = yamlProblems(text).filter((p) => p.severity === "error");
  const planIsCurrent = planned?.text === text && planned.response.ok;

  const run = (dryRun: boolean) => {
    setError(null);
    apply.mutate(
      { manifest: text, namespace, dry_run: dryRun },
      {
        onSuccess: (response) => {
          if (dryRun) {
            setPlanned({ text, response });
            setApplied(null);
          } else {
            setApplied(response);
            setPlanned(null);
            if (response.ok) toast.success("Applied to the cluster");
            else toast.error("Apply stopped at the first error");
            onApplied?.(response);
          }
        },
        onError: (e) => setError(e instanceof ApiError ? e.message : String(e)),
      },
    );
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-3">
        {allowNamespace && (
          <div className="space-y-1">
            <Label htmlFor="apply-ns" className="text-xs text-muted-foreground">Default namespace</Label>
            <Input id="apply-ns" value={namespace} onChange={(e) => setNamespace(e.target.value)} className="h-8 w-44 font-mono" />
          </div>
        )}
        {allowFiles && (
          <>
            <label className="inline-flex h-8 cursor-pointer items-center gap-1.5 rounded-md border border-border px-2.5 text-sm hover:bg-muted/40">
              <FileUp className="size-4" /> Import file
              <input
                type="file"
                accept=".yaml,.yml,.json"
                className="sr-only"
                onChange={async (e) => {
                  const file = e.target.files?.[0];
                  if (file) setText(await file.text());
                }}
              />
            </label>
            <Button
              variant="outline"
              size="sm"
              onClick={() => {
                const url = URL.createObjectURL(new Blob([text], { type: "application/yaml" }));
                const a = Object.assign(document.createElement("a"), { href: url, download: "manifest.yaml" });
                a.click();
                URL.revokeObjectURL(url);
              }}
            >
              <Download /> Export
            </Button>
          </>
        )}
        <div className="ml-auto flex gap-2">
          <Button variant="outline" size="sm" disabled={syntax.length > 0 || !text.trim() || apply.isPending} onClick={() => run(true)}>
            <FlaskConical /> {apply.isPending && !planIsCurrent ? "Checking…" : "Dry run & diff"}
          </Button>
          <Button size="sm" disabled={!planIsCurrent || apply.isPending} onClick={() => run(false)} title={planIsCurrent ? "" : "Run a successful dry run of this exact YAML first"}>
            <Rocket /> {apply.isPending && planIsCurrent ? "Applying…" : "Apply"}
          </Button>
        </div>
      </div>
      <YamlEditor value={text} onChange={setText} height={height} />
      {syntax.length > 0 && (
        <p className="text-xs text-red-300">YAML syntax: {syntax[0].message} {syntax.length > 1 ? `(+${syntax.length - 1} more)` : ""}</p>
      )}
      {error && <p role="alert" className="rounded border border-red-500/30 bg-red-500/5 p-2 text-sm text-red-300">{error}</p>}
      {planned && planned.text !== text && <p className="text-xs text-amber-300">The YAML changed since the last dry run — run it again before applying.</p>}
      {planned && <PlanResults response={planned.response} />}
      {applied && <PlanResults response={applied} />}
    </div>
  );
}
