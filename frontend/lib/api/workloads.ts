"use client";

// Workload management (Phase 2): resources, actions, YAML plan/apply, secrets, logs, terminals.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, apiRequest, buildUrl } from "./client";
import type { KubeEvent, KubePod } from "./kube";

export type ResourceKind =
  | "pods"
  | "deployments"
  | "replicasets"
  | "statefulsets"
  | "daemonsets"
  | "jobs"
  | "cronjobs"
  | "services"
  | "ingresses"
  | "networkpolicies"
  | "endpointslices"
  | "persistentvolumes"
  | "persistentvolumeclaims"
  | "storageclasses"
  | "csidrivers"
  | "volumeattachments"
  | "gatewayclasses"
  | "gateways"
  | "httproutes"
  | "referencegrants"
  | "resourcequotas"
  | "limitranges"
  | "configmaps"
  | "secrets";

/** A list row; fields vary by kind (see backend workloads/summaries.py). */
export type ResourceRow = Record<string, unknown> & { name: string; namespace: string | null; created: string | null };

export interface Rollout {
  state: "complete" | "progressing" | "failed" | "paused";
  message: string;
}

export interface Revision {
  revision: number | null;
  replicaset: string;
  created: string;
  images: string[];
  change_cause: string | null;
  replicas: number;
  current: boolean;
}

export interface ResourceDetail {
  kind: string;
  summary: ResourceRow;
  object: Record<string, unknown>;
  yaml: string;
  events: KubeEvent[];
  capabilities: { scale: boolean; restart: boolean; delete: boolean; namespaced: boolean };
  scaling_policy?: Record<string, unknown> | null;
  rollout?: Rollout;
  history?: Revision[];
  pods?: KubePod[];
  containers?: string[];
  init_containers?: string[];
  jobs?: ResourceRow[];
}

export interface PlanResult {
  kind: string;
  name: string;
  namespace: string | null;
  action: "create" | "update" | "unchanged" | "error";
  diff?: string;
  applied: boolean;
  error?: string;
  causes?: { field: string | null; message: string | null; reason: string | null }[];
  note?: string;
}

export interface ApplyResponse {
  dry_run: boolean;
  ok: boolean;
  results: PlanResult[];
}

const base = (clusterId: string) => `/clusters/${clusterId}`;
const keys = {
  list: (cid: string, kind: string, ns: string) => ["clusters", cid, "workloads", kind, ns] as const,
  item: (cid: string, kind: string, ns: string, name: string) => ["clusters", cid, "workloads", kind, ns, name] as const,
};

export function useResources(clusterId: string, kind: ResourceKind, namespace: string) {
  return useQuery({
    queryKey: keys.list(clusterId, kind, namespace),
    queryFn: () => api.get<{ items: ResourceRow[]; truncated: boolean }>(`${base(clusterId)}/resources/${kind}`, { namespace: namespace || undefined }),
    refetchInterval: 10_000,
    retry: false,
  });
}

export function useResource(clusterId: string, kind: ResourceKind, namespace: string | null, name: string | null) {
  return useQuery({
    queryKey: keys.item(clusterId, kind, namespace ?? "", name ?? ""),
    queryFn: () => api.get<ResourceDetail>(`${base(clusterId)}/resources/${kind}/${namespace}/${name}`),
    enabled: Boolean(namespace && name),
    refetchInterval: 5_000,
    retry: false,
  });
}

function useInvalidate(clusterId: string) {
  const client = useQueryClient();
  return () => client.invalidateQueries({ queryKey: ["clusters", clusterId] });
}

export function useScale(clusterId: string, kind: ResourceKind) {
  const invalidate = useInvalidate(clusterId);
  return useMutation({
    mutationFn: ({ namespace, name, replicas }: { namespace: string; name: string; replicas: number }) =>
      api.post<{ replicas: number; previous: number }>(`${base(clusterId)}/resources/${kind}/${namespace}/${name}/scale`, { replicas }),
    onSuccess: invalidate,
  });
}

export function useSetScalingPolicy(clusterId: string) {
  const invalidate = useInvalidate(clusterId);
  return useMutation({
    mutationFn: ({ namespace, name, ...body }: { namespace: string; name: string; minimum: number; maximum: number; metric: "cpu" | "memory"; target_utilization: number; scale_down_stabilization_seconds: number }) =>
      api.put<Record<string, unknown>>(`${base(clusterId)}/deployments/${namespace}/${name}/scaling-policy`, body),
    onSuccess: invalidate,
  });
}

export function useDeleteScalingPolicy(clusterId: string) {
  const invalidate = useInvalidate(clusterId);
  return useMutation({
    mutationFn: ({ namespace, name }: { namespace: string; name: string }) =>
      apiRequest<void>(`${base(clusterId)}/deployments/${namespace}/${name}/scaling-policy`, { method: "DELETE" }),
    onSuccess: invalidate,
  });
}

export function useRestart(clusterId: string, kind: ResourceKind) {
  const invalidate = useInvalidate(clusterId);
  return useMutation({
    mutationFn: ({ namespace, name }: { namespace: string; name: string }) =>
      api.post<{ restarted_at: string }>(`${base(clusterId)}/resources/${kind}/${namespace}/${name}/restart`),
    onSuccess: invalidate,
  });
}

export function useDeleteResource(clusterId: string, kind: ResourceKind) {
  const invalidate = useInvalidate(clusterId);
  return useMutation({
    mutationFn: ({ namespace, name }: { namespace: string; name: string }) =>
      apiRequest<void>(`${base(clusterId)}/resources/${kind}/${namespace}/${name}`, { method: "DELETE", query: { confirm: name } }),
    onSuccess: invalidate,
  });
}

export function useRollback(clusterId: string) {
  const invalidate = useInvalidate(clusterId);
  return useMutation({
    mutationFn: ({ namespace, name, revision }: { namespace: string; name: string; revision: number }) =>
      api.post<{ rolled_back_to: number; images: string[] }>(`${base(clusterId)}/deployments/${namespace}/${name}/rollback`, { revision }),
    onSuccess: invalidate,
  });
}

export function useCronJobTrigger(clusterId: string) {
  const invalidate = useInvalidate(clusterId);
  return useMutation({
    mutationFn: ({ namespace, name }: { namespace: string; name: string }) =>
      api.post<{ job: string }>(`${base(clusterId)}/cronjobs/${namespace}/${name}/trigger`),
    onSuccess: invalidate,
  });
}

export function useCronJobSuspend(clusterId: string) {
  const invalidate = useInvalidate(clusterId);
  return useMutation({
    mutationFn: ({ namespace, name, suspend }: { namespace: string; name: string; suspend: boolean }) =>
      api.post<{ suspend: boolean }>(`${base(clusterId)}/cronjobs/${namespace}/${name}/suspend`, { suspend }),
    onSuccess: invalidate,
  });
}

export function useApply(clusterId: string) {
  const invalidate = useInvalidate(clusterId);
  return useMutation({
    mutationFn: (body: { manifest: string; namespace: string; dry_run: boolean }) =>
      api.post<ApplyResponse>(`${base(clusterId)}/apply`, body),
    onSuccess: (data) => {
      if (!data.dry_run) void invalidate();
    },
  });
}

export function useRevealSecret(clusterId: string) {
  return useMutation({
    mutationFn: ({ namespace, name }: { namespace: string; name: string }) =>
      api.post<{ values: Record<string, { text: string; binary: boolean }> }>(`${base(clusterId)}/secrets/${namespace}/${name}/reveal`),
  });
}

export async function fetchLogs(
  clusterId: string,
  namespace: string,
  pod: string,
  options: { container?: string; tail: number; previous: boolean; timestamps: boolean },
): Promise<string> {
  const response = await fetch(
    buildUrl(`${base(clusterId)}/pods/${namespace}/${pod}/logs`, {
      container: options.container,
      tail: options.tail,
      previous: options.previous || undefined,
      timestamps: options.timestamps || undefined,
    }),
  );
  if (!response.ok) {
    let message = `Could not load logs (HTTP ${response.status})`;
    try {
      message = (await response.json()).error.message;
    } catch {
      /* keep default */
    }
    throw new Error(message);
  }
  return response.text();
}

export function logStreamUrl(clusterId: string, namespace: string, pod: string, container?: string, tail = 200): string {
  return buildUrl(`${base(clusterId)}/pods/${namespace}/${pod}/logs/stream`, { container, tail });
}

export function terminalUrl(clusterId: string, namespace: string, pod: string, container?: string): string {
  const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${scheme}//${window.location.host}${buildUrl(`${base(clusterId)}/pods/${namespace}/${pod}/exec`, { container })}`;
}
