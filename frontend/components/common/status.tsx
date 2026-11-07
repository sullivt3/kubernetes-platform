import { CheckCircle2, CircleDashed, CircleSlash, Clock, Loader2, XCircle, type LucideIcon } from "lucide-react";

import { cn } from "@/lib/utils";
import type { HealthStatus, ImplementationStatus, OperationStatus, StepStatus } from "@/lib/api/types";

type Tone = "success" | "warning" | "danger" | "info" | "neutral";

const TONE_CLASSES: Record<Tone, string> = {
  success: "border-emerald-500/30 bg-emerald-500/10 text-emerald-400",
  warning: "border-amber-500/30 bg-amber-500/10 text-amber-400",
  danger: "border-red-500/30 bg-red-500/10 text-red-400",
  info: "border-sky-500/30 bg-sky-500/10 text-sky-400",
  neutral: "border-border bg-muted/40 text-muted-foreground",
};

const DOT_CLASSES: Record<Tone, string> = {
  success: "bg-emerald-400",
  warning: "bg-amber-400",
  danger: "bg-red-400",
  info: "bg-sky-400",
  neutral: "bg-muted-foreground",
};

export function StatusPill({
  tone,
  icon: Icon,
  spin,
  children,
  className,
}: {
  tone: Tone;
  icon?: LucideIcon;
  spin?: boolean;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-md border px-2 py-0.5 text-xs font-medium whitespace-nowrap",
        TONE_CLASSES[tone],
        className,
      )}
    >
      {Icon && <Icon className={cn("size-3.5", spin && "animate-spin")} aria-hidden />}
      {children}
    </span>
  );
}

export function StatusDot({ tone, pulse }: { tone: Tone; pulse?: boolean }) {
  return (
    <span className="relative inline-flex size-2">
      {pulse && <span className={cn("absolute inset-0 animate-ping rounded-full opacity-60", DOT_CLASSES[tone])} />}
      <span className={cn("relative inline-flex size-2 rounded-full", DOT_CLASSES[tone])} />
    </span>
  );
}

const OPERATION: Record<OperationStatus, { tone: Tone; icon: LucideIcon; spin?: boolean; label: string }> = {
  pending: { tone: "neutral", icon: Clock, label: "Pending" },
  queued: { tone: "info", icon: Clock, label: "Queued" },
  running: { tone: "info", icon: Loader2, spin: true, label: "Running" },
  cancelling: { tone: "warning", icon: Loader2, spin: true, label: "Cancelling" },
  succeeded: { tone: "success", icon: CheckCircle2, label: "Succeeded" },
  failed: { tone: "danger", icon: XCircle, label: "Failed" },
  cancelled: { tone: "warning", icon: CircleSlash, label: "Cancelled" },
};

export function OperationStatusBadge({ status }: { status: OperationStatus }) {
  const s = OPERATION[status];
  return (
    <StatusPill tone={s.tone} icon={s.icon} spin={s.spin}>
      {s.label}
    </StatusPill>
  );
}

export const STEP_STYLE: Record<StepStatus, { tone: Tone; icon: LucideIcon; spin?: boolean; label: string }> = {
  pending: { tone: "neutral", icon: CircleDashed, label: "Pending" },
  running: { tone: "info", icon: Loader2, spin: true, label: "Running" },
  succeeded: { tone: "success", icon: CheckCircle2, label: "Succeeded" },
  failed: { tone: "danger", icon: XCircle, label: "Failed" },
  skipped: { tone: "neutral", icon: CircleSlash, label: "Skipped" },
  cancelled: { tone: "warning", icon: CircleSlash, label: "Cancelled" },
};

export const HEALTH_TONE: Record<HealthStatus, Tone> = {
  ok: "success",
  degraded: "warning",
  unavailable: "danger",
};

export function HealthBadge({ status }: { status: HealthStatus }) {
  const label = { ok: "Healthy", degraded: "Degraded", unavailable: "Unavailable" }[status];
  return (
    <StatusPill tone={HEALTH_TONE[status]}>
      <StatusDot tone={HEALTH_TONE[status]} />
      {label}
    </StatusPill>
  );
}

export function ImplementationBadge({ status, phase }: { status: ImplementationStatus; phase: number | null }) {
  if (status === "available") return <StatusPill tone="success">Available</StatusPill>;
  if (status === "preview") return <StatusPill tone="info">Preview</StatusPill>;
  return <StatusPill tone="neutral">{phase ? `Planned · Phase ${phase}` : "Planned"}</StatusPill>;
}
