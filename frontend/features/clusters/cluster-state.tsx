import { AlertTriangle, CheckCircle2, Clock, Loader2, Trash2, XCircle } from "lucide-react";

import { StatusPill } from "@/components/common/status";

export function ClusterStateBadge({ state }: { state: string }) {
  switch (state) {
    case "active":
      return <StatusPill tone="success" icon={CheckCircle2}>Active</StatusPill>;
    case "pending":
      return <StatusPill tone="neutral" icon={Clock}>Pending</StatusPill>;
    case "provisioning":
      return <StatusPill tone="info" icon={Loader2} spin>Installing</StatusPill>;
    case "upgrading":
      return <StatusPill tone="info" icon={Loader2} spin>Upgrading</StatusPill>;
    case "deleting":
      return <StatusPill tone="warning" icon={Trash2}>Deleting</StatusPill>;
    case "degraded":
    case "unreachable":
      return <StatusPill tone="warning" icon={AlertTriangle}>{state[0].toUpperCase() + state.slice(1)}</StatusPill>;
    case "error":
      return <StatusPill tone="danger" icon={XCircle}>Error</StatusPill>;
    default:
      return <StatusPill tone="neutral">{state}</StatusPill>;
  }
}
