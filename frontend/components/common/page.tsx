import { AlertTriangle, Inbox, RefreshCw } from "lucide-react";
import type { ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiError } from "@/lib/api/client";
import { cn } from "@/lib/utils";

export function PageHeader({
  title,
  description,
  actions,
  eyebrow,
}: {
  title: string;
  description?: ReactNode;
  actions?: ReactNode;
  eyebrow?: string;
}) {
  return (
    <div className="flex flex-col gap-4 border-b border-border/60 pb-5 sm:flex-row sm:items-end sm:justify-between">
      <div className="min-w-0 space-y-1">
        {eyebrow && <p className="text-xs font-medium tracking-wide text-muted-foreground uppercase">{eyebrow}</p>}
        <h1 className="text-xl font-semibold tracking-tight">{title}</h1>
        {description && <p className="max-w-3xl text-sm text-muted-foreground">{description}</p>}
      </div>
      {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
    </div>
  );
}

export function EmptyState({
  title,
  description,
  icon: Icon = Inbox,
  action,
  className,
}: {
  title: string;
  description?: ReactNode;
  icon?: React.ComponentType<{ className?: string }>;
  action?: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center gap-3 rounded-lg border border-dashed border-border/70 px-6 py-12 text-center",
        className,
      )}
    >
      <Icon className="size-8 text-muted-foreground/70" />
      <div className="space-y-1">
        <p className="text-sm font-medium">{title}</p>
        {description && <p className="mx-auto max-w-md text-sm text-muted-foreground">{description}</p>}
      </div>
      {action}
    </div>
  );
}

export function ErrorState({
  error,
  onRetry,
  title = "Could not load data",
  className,
}: {
  error: unknown;
  onRetry?: () => void;
  title?: string;
  className?: string;
}) {
  const apiError = error instanceof ApiError ? error : null;
  const message = error instanceof Error ? error.message : "An unexpected error occurred.";
  return (
    <div
      role="alert"
      className={cn(
        "flex flex-col gap-3 rounded-lg border border-red-500/30 bg-red-500/5 p-4 sm:flex-row sm:items-center sm:justify-between",
        className,
      )}
    >
      <div className="flex gap-3">
        <AlertTriangle className="mt-0.5 size-4 shrink-0 text-red-400" />
        <div className="space-y-0.5 text-sm">
          <p className="font-medium text-red-300">{title}</p>
          <p className="text-muted-foreground">{message}</p>
          {apiError?.requestId && (
            <p className="font-mono text-xs text-muted-foreground/80">Request ID: {apiError.requestId}</p>
          )}
        </div>
      </div>
      {onRetry && (
        <Button variant="outline" size="sm" onClick={onRetry}>
          <RefreshCw /> Retry
        </Button>
      )}
    </div>
  );
}

export function TableSkeleton({ rows = 5, columns = 4 }: { rows?: number; columns?: number }) {
  return (
    <div className="space-y-2" aria-busy="true" aria-label="Loading">
      {Array.from({ length: rows }).map((_, r) => (
        <div key={r} className="flex gap-4">
          {Array.from({ length: columns }).map((_, c) => (
            <Skeleton key={c} className="h-8 flex-1" />
          ))}
        </div>
      ))}
    </div>
  );
}

export function Section({
  title,
  description,
  actions,
  children,
  className,
}: {
  title: string;
  description?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={cn("space-y-3", className)}>
      <div className="flex items-end justify-between gap-4">
        <div>
          <h2 className="text-sm font-semibold">{title}</h2>
          {description && <p className="text-xs text-muted-foreground">{description}</p>}
        </div>
        {actions}
      </div>
      {children}
    </section>
  );
}
