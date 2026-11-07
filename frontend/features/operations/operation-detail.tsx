"use client";

import { ArrowLeft, Ban, ChevronRight, RotateCcw } from "lucide-react";
import Link from "next/link";
import { use, useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";

import { ConfirmDialog } from "@/components/common/confirm-dialog";
import { ErrorState, PageHeader, Section } from "@/components/common/page";
import { OperationStatusBadge, STEP_STYLE, StatusDot, StatusPill } from "@/components/common/status";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Progress } from "@/components/ui/progress";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiError } from "@/lib/api/client";
import { useCancelOperation, useOperation, useRetryOperation } from "@/lib/api/hooks";
import type { OperationDetail as Operation, OperationLog } from "@/lib/api/types";
import { formatDateTime, formatDuration, formatTime } from "@/lib/format";
import { cn } from "@/lib/utils";
import { useOperationStream, type StreamState } from "./use-operation-stream";

const ACTIVE = new Set(["pending", "queued", "running", "cancelling"]);

const LEVEL_CLASS: Record<OperationLog["level"], string> = {
  debug: "text-muted-foreground/70",
  info: "text-foreground/90",
  warning: "text-amber-300",
  error: "text-red-400",
};

function StepsTimeline({ operation }: { operation: Operation }) {
  return (
    <ol className="space-y-1">
      {operation.steps.map((step, index) => {
        const style = STEP_STYLE[step.status];
        const Icon = style.icon;
        return (
          <li key={step.key} className="flex gap-3 rounded-md px-2 py-2 hover:bg-muted/30">
            <div className="flex flex-col items-center">
              <Icon
                className={cn(
                  "size-4.5 shrink-0",
                  style.spin && "animate-spin",
                  {
                    success: "text-emerald-400",
                    danger: "text-red-400",
                    info: "text-sky-400",
                    warning: "text-amber-400",
                    neutral: "text-muted-foreground",
                  }[style.tone],
                )}
              />
              {index < operation.steps.length - 1 && <span className="mt-1 w-px flex-1 bg-border" />}
            </div>
            <div className="min-w-0 flex-1 pb-1">
              <div className="flex items-baseline justify-between gap-3">
                <p className="text-sm font-medium">{step.name}</p>
                <span className="shrink-0 text-xs tabular-nums text-muted-foreground">
                  {step.started_at ? formatDuration(step.started_at, step.finished_at) : style.label}
                </span>
              </div>
              <p className="font-mono text-[11px] text-muted-foreground">
                {step.key}
                {step.attempts > 1 && ` · ${step.attempts} attempts`}
              </p>
              {step.error && (
                <p className="mt-1 rounded border border-red-500/30 bg-red-500/5 px-2 py-1 text-xs text-red-300">
                  {step.error.message}
                </p>
              )}
            </div>
          </li>
        );
      })}
    </ol>
  );
}

function StreamBadge({ state }: { state: StreamState }) {
  const map = {
    connecting: { tone: "neutral", label: "Connecting" },
    live: { tone: "success", label: "Live" },
    polling: { tone: "warning", label: "Polling" },
    ended: { tone: "neutral", label: "Complete" },
  } as const;
  const s = map[state];
  return (
    <StatusPill tone={s.tone}>
      <StatusDot tone={s.tone} pulse={state === "live"} />
      {s.label}
    </StatusPill>
  );
}

function LogViewer({ logs, state }: { logs: OperationLog[]; state: StreamState }) {
  const [showDebug, setShowDebug] = useState(false);
  const [follow, setFollow] = useState(true);
  const container = useRef<HTMLDivElement>(null);
  const visible = useMemo(() => logs.filter((l) => showDebug || l.level !== "debug"), [logs, showDebug]);
  const hiddenDebug = logs.length - visible.length;

  useEffect(() => {
    if (follow && container.current) container.current.scrollTop = container.current.scrollHeight;
  }, [visible, follow]);

  return (
    <Section
      title="Execution log"
      description="Persisted and streamed in real time; secrets registered by steps are redacted."
      actions={
        <div className="flex items-center gap-2">
          <Button variant="ghost" size="xs" onClick={() => setShowDebug((v) => !v)}>
            {showDebug ? "Hide debug" : `Show debug${hiddenDebug ? ` (${hiddenDebug})` : ""}`}
          </Button>
          <StreamBadge state={state} />
        </div>
      }
    >
      <div
        ref={container}
        onScroll={(e) => {
          const el = e.currentTarget;
          setFollow(el.scrollHeight - el.scrollTop - el.clientHeight < 40);
        }}
        className="h-[420px] overflow-y-auto rounded-lg border border-border bg-black/40 p-3 font-mono text-xs leading-relaxed"
        role="log"
        aria-live="polite"
      >
        {visible.length === 0 && <p className="text-muted-foreground">Waiting for log output…</p>}
        {visible.map((log) => (
          <div key={log.id} className="flex gap-3 whitespace-pre-wrap break-words">
            <span className="shrink-0 text-muted-foreground/60 select-none">{formatTime(log.ts)}</span>
            <span className="w-28 shrink-0 truncate text-sky-400/70 select-none">{log.step_key ?? "operation"}</span>
            <span className={LEVEL_CLASS[log.level]}>{log.message}</span>
          </div>
        ))}
      </div>
    </Section>
  );
}

function JsonBlock({ title, value }: { title: string; value: unknown }) {
  return (
    <Collapsible>
      <CollapsibleTrigger className="group flex items-center gap-1.5 text-sm font-medium">
        <ChevronRight className="size-4 transition-transform group-data-[state=open]:rotate-90" />
        {title}
      </CollapsibleTrigger>
      <CollapsibleContent>
        <pre className="mt-2 max-h-80 overflow-auto rounded-lg border border-border bg-black/40 p-3 font-mono text-xs">
          {JSON.stringify(value, null, 2)}
        </pre>
      </CollapsibleContent>
    </Collapsible>
  );
}

function Facts({ operation }: { operation: Operation }) {
  const facts: [string, React.ReactNode][] = [
    ["Type", <span key="t" className="font-mono">{operation.type}</span>],
    ["Target", operation.target_name ?? operation.target_id ?? "—"],
    ["Requested by", <span key="r" className="font-mono">{operation.requested_by_subject}</span>],
    ["Attempt", operation.recovery_count ? `${operation.attempt} (recovered ${operation.recovery_count}×)` : operation.attempt],
    ["Created", formatDateTime(operation.created_at)],
    ["Started", formatDateTime(operation.started_at)],
    ["Finished", formatDateTime(operation.finished_at)],
    ["Duration", formatDuration(operation.started_at, operation.finished_at)],
    ["Worker", <span key="w" className="font-mono">{operation.worker_id ?? "—"}</span>],
    ["Last heartbeat", formatDateTime(operation.heartbeat_at)],
  ];
  return (
    <dl className="grid gap-x-6 gap-y-2 text-sm sm:grid-cols-2">
      {facts.map(([label, value]) => (
        <div key={label} className="flex justify-between gap-4 border-b border-border/40 pb-1.5">
          <dt className="text-muted-foreground">{label}</dt>
          <dd className="truncate text-right">{value}</dd>
        </div>
      ))}
    </dl>
  );
}

export function OperationDetailView({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const operation = useOperation(id);
  const [generation, setGeneration] = useState(0);
  const stream = useOperationStream(id, generation);
  const cancel = useCancelOperation();
  const retry = useRetryOperation();
  const [confirmCancel, setConfirmCancel] = useState(false);

  if (operation.isLoading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-16" />
        <Skeleton className="h-96" />
      </div>
    );
  }
  if (operation.isError || !operation.data) {
    const notFound = operation.error instanceof ApiError && operation.error.status === 404;
    return (
      <div className="space-y-4">
        <BackLink />
        <ErrorState
          title={notFound ? "Operation not found" : "Could not load operation"}
          error={operation.error}
          onRetry={notFound ? undefined : () => operation.refetch()}
        />
      </div>
    );
  }

  const op = operation.data;
  const active = ACTIVE.has(op.status);
  const canCancel = active && op.status !== "cancelling";
  const canRetry = op.status === "failed" || op.status === "cancelled";
  const onError = (title: string) => (error: unknown) =>
    toast.error(title, { description: error instanceof Error ? error.message : String(error) });

  return (
    <div className="space-y-6">
      <BackLink />
      <PageHeader
        eyebrow="Operation"
        title={op.title}
        description={<span className="font-mono text-xs">{op.id}</span>}
        actions={
          <>
            <OperationStatusBadge status={op.status} />
            {canCancel && (
              <Button variant="outline" size="sm" onClick={() => setConfirmCancel(true)}>
                <Ban /> Cancel
              </Button>
            )}
            {canRetry && (
              <Button
                size="sm"
                disabled={retry.isPending}
                onClick={() =>
                  retry.mutate(op.id, {
                    onSuccess: () => setGeneration((g) => g + 1),
                    onError: onError("Retry failed"),
                  })
                }
              >
                <RotateCcw /> {retry.isPending ? "Retrying…" : "Retry from failed step"}
              </Button>
            )}
          </>
        }
      />

      <div className="flex items-center gap-3">
        <Progress value={op.progress} className="h-2" />
        <span className="w-12 text-right text-sm tabular-nums text-muted-foreground">{Math.round(op.progress)}%</span>
      </div>

      {op.error && (
        <ErrorState
          title={`Failed${op.error.step ? ` at step "${op.error.step}"` : ""} (${op.error.code})`}
          error={new Error(op.error.message)}
        />
      )}

      <div className="grid gap-6 lg:grid-cols-5">
        <Card className="lg:col-span-2">
          <CardContent className="space-y-4">
            <h2 className="text-sm font-semibold">Steps</h2>
            <StepsTimeline operation={op} />
          </CardContent>
        </Card>
        <div className="space-y-6 lg:col-span-3">
          <LogViewer logs={stream.logs} state={stream.state} />
        </div>
      </div>

      <Card>
        <CardContent className="space-y-4">
          <h2 className="text-sm font-semibold">Details</h2>
          <Facts operation={op} />
          <div className="space-y-2 pt-2">
            {op.result && <JsonBlock title="Result" value={op.result} />}
            <JsonBlock title="Parameters" value={op.params} />
          </div>
        </CardContent>
      </Card>

      <ConfirmDialog
        open={confirmCancel}
        onOpenChange={setConfirmCancel}
        title="Cancel this operation?"
        description={
          <p>
            Queued operations stop immediately. Running operations stop at the next safe point; the current step
            finishes or is interrupted cleanly. Completed steps are not rolled back.
          </p>
        }
        confirmLabel="Cancel operation"
        pending={cancel.isPending}
        onConfirm={() =>
          cancel.mutate(op.id, {
            onSuccess: () => setConfirmCancel(false),
            onError: onError("Cancellation failed"),
          })
        }
      />
    </div>
  );
}

function BackLink() {
  return (
    <Button asChild variant="ghost" size="sm" className="-ml-2">
      <Link href="/operations">
        <ArrowLeft /> All operations
      </Link>
    </Button>
  );
}
