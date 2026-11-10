"use client";

import Link from "next/link";

import { ErrorState } from "@/components/common/page";
import { usePhase6Operation } from "@/lib/api/phase6";

export function Phase6Operation({ id }: { id: string | null }) {
  const operation = usePhase6Operation(id);
  if (!id) return null;
  if (operation.isError) return <ErrorState error={operation.error} onRetry={() => operation.refetch()} />;
  const op = operation.data;
  return (
    <div className="space-y-2 rounded-lg border p-4 text-sm" aria-live="polite">
      <Link className="font-medium underline underline-offset-4" href={`/operations/${id}`}>{op?.title ?? "Loading operation…"}</Link>
      {op && <p className="text-muted-foreground">{op.status} · {Math.round(op.progress)}%</p>}
      {op?.error && <p className="text-destructive">{op.error.message}</p>}
      {op?.result && typeof op.result.resolved === "boolean" && (
        <p className={op.result.resolved ? "text-emerald-400" : "text-amber-400"}>
          {op.result.resolved ? "DNS query succeeded" : "DNS query did not resolve"}
        </p>
      )}
      {typeof op?.result?.output === "string" && <pre className="max-h-80 overflow-auto whitespace-pre-wrap rounded bg-muted p-3 font-mono text-xs">{op.result.output}</pre>}
      <p className="text-xs text-muted-foreground">Open the operation for live logs, cancellation, or retry after a failure.</p>
    </div>
  );
}
