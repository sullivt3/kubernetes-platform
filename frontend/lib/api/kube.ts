"use client";

// Live Kubernetes data and cluster connectivity (Phase 1).

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { ApiError, api, apiRequest } from "./client";
import { queryKeys } from "./hooks";
import type { OperationDetail } from "./types";

export interface ResourceUsage {
  allocatable_m?: number;
  requested_m?: number;
  used_m?: number | null;
  allocatable_bytes?: number;
  requested_bytes?: number;
  used_bytes?: number | null;
}

export interface ClusterAlert {
  severity: "critical" | "warning";
  message: string;
  pods?: string[];
}

export interface ClusterHealth {
  version: string | null;
  platform?: string | null;
  api_ready: boolean;
  nodes: { total: number; ready: number; not_ready: string[] };
  pods: { total: number; running: number; pending: number; failed: number; succeeded: number; problems: number; restarts: number };
  cpu: ResourceUsage;
  memory: ResourceUsage;
  metrics_available: boolean;
  alerts: ClusterAlert[];
  readyz_checks?: { name: string; ok: boolean }[];
  pods_truncated?: boolean;
}

export interface NodeCondition {
  type: string;
  status: string;
  reason: string | null;
  message: string | null;
  last_transition: string | null;
}

export interface KubeNode {
  name: string;
  roles: string[];
  ready: boolean;
  status: string;
  problems: string[];
  unschedulable: boolean;
  kubelet_version: string;
  os_image: string;
  kernel: string;
  container_runtime: string;
  architecture: string;
  internal_ip: string | null;
  external_ip: string | null;
  created: string;
  labels: Record<string, string>;
  taints: { key: string; value: string | null; effect: string }[];
  cpu: { capacity_m: number; allocatable_m: number; requested_m: number; used_m: number | null };
  memory: { capacity_bytes: number; allocatable_bytes: number; requested_bytes: number; used_bytes: number | null };
  pods: { count: number; capacity: number };
  conditions: NodeCondition[];
}

export interface KubePod {
  name: string;
  namespace: string;
  phase: string;
  status: string;
  problem: boolean;
  ready: string;
  restarts: number;
  node: string | null;
  pod_ip: string | null;
  created: string;
  owner_kind: string | null;
  owner_name: string | null;
  images: string[];
  qos_class: string | null;
}

export interface KubeEvent {
  namespace: string | null;
  type: string;
  reason: string;
  message: string;
  object: string;
  count: number;
  last_seen: string | null;
  source: string | null;
}

export interface KubeNodeDetail extends KubeNode {
  pod_list: KubePod[];
  events: KubeEvent[];
}

export interface KubeNamespace {
  name: string;
  status: string;
  created: string;
  labels: Record<string, string>;
  pods: { total: number; running?: number; pending?: number; failed?: number; succeeded?: number };
  problem_pods: number;
}

export interface Topology {
  nodes: (Pick<KubeNode, "name" | "roles" | "ready" | "status" | "internal_ip" | "cpu" | "memory" | "pods"> & { pod_list: KubePod[] })[];
  unscheduled: KubePod[];
  truncated: boolean;
}

export interface KubeconfigContext {
  name: string;
  cluster: string;
  user: string;
  server: string | null;
  namespace: string | null;
  auth_type: string;
  supported: boolean;
  problem: string | null;
}

export function isUnreachable(error: unknown): boolean {
  return error instanceof ApiError && (error.code === "cluster_unreachable" || error.code === "cluster_auth_failed");
}

const live = (key: string, id: string, extra: unknown[] = []) => ["clusters", id, "live", key, ...extra] as const;
const LIVE_OPTIONS = { refetchInterval: 15_000, retry: false, staleTime: 5_000 } as const;

export function useClusterHealth(id: string | null) {
  return useQuery({
    queryKey: live("health", id ?? ""),
    queryFn: () => api.get<ClusterHealth>(`/clusters/${id}/health`),
    enabled: Boolean(id),
    ...LIVE_OPTIONS,
    refetchInterval: 30_000,
  });
}

export function useClusterNodes(id: string | null) {
  return useQuery({
    queryKey: live("nodes", id ?? ""),
    queryFn: () => api.get<KubeNode[]>(`/clusters/${id}/nodes`),
    enabled: Boolean(id),
    ...LIVE_OPTIONS,
  });
}

export function useClusterNode(id: string | null, name: string | null) {
  return useQuery({
    queryKey: live("node", id ?? "", [name]),
    queryFn: () => api.get<KubeNodeDetail>(`/clusters/${id}/nodes/${name}`),
    enabled: Boolean(id && name),
    ...LIVE_OPTIONS,
  });
}

export function useClusterNamespaces(id: string | null) {
  return useQuery({
    queryKey: live("namespaces", id ?? ""),
    queryFn: () => api.get<KubeNamespace[]>(`/clusters/${id}/namespaces`),
    enabled: Boolean(id),
    ...LIVE_OPTIONS,
  });
}

export function useClusterPods(id: string | null, filters: { namespace?: string; node?: string; problems?: boolean }) {
  return useQuery({
    queryKey: live("pods", id ?? "", [filters]),
    queryFn: () =>
      api.get<{ items: KubePod[]; truncated: boolean }>(`/clusters/${id}/pods`, {
        namespace: filters.namespace || undefined,
        node: filters.node || undefined,
        problems: filters.problems || undefined,
      }),
    enabled: Boolean(id),
    ...LIVE_OPTIONS,
  });
}

export function useClusterEvents(id: string | null, filters: { namespace?: string; warnings?: boolean }) {
  return useQuery({
    queryKey: live("events", id ?? "", [filters]),
    queryFn: () =>
      api.get<KubeEvent[]>(`/clusters/${id}/events`, {
        namespace: filters.namespace || undefined,
        warnings: filters.warnings || undefined,
        limit: 200,
      }),
    enabled: Boolean(id),
    ...LIVE_OPTIONS,
  });
}

export function useClusterTopology(id: string | null) {
  return useQuery({
    queryKey: live("topology", id ?? ""),
    queryFn: () => api.get<Topology>(`/clusters/${id}/topology`),
    enabled: Boolean(id),
    ...LIVE_OPTIONS,
  });
}

// ---- connectivity mutations -----------------------------------------------------

function useInvalidateClusters() {
  const client = useQueryClient();
  return () => {
    void client.invalidateQueries({ queryKey: queryKeys.clusters });
    void client.invalidateQueries({ queryKey: queryKeys.operations });
  };
}

export function useInspectKubeconfig() {
  return useMutation({
    mutationFn: (kubeconfig: string) =>
      api.post<{ current_context: string | null; contexts: KubeconfigContext[] }>("/clusters/import/inspect", { kubeconfig }),
  });
}

export interface ClusterImportBody {
  name: string;
  kubeconfig: string;
  context?: string | null;
  server_override?: string | null;
  tls_server_name?: string | null;
  description?: string | null;
}

export function useImportCluster() {
  const invalidate = useInvalidateClusters();
  return useMutation({
    mutationFn: (body: ClusterImportBody) =>
      api.post<{ cluster_id: string; operation: OperationDetail }>("/clusters/import", body),
    onSuccess: invalidate,
  });
}

export function useUpdateConnection(id: string) {
  const invalidate = useInvalidateClusters();
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: { server_override: string | null; tls_server_name: string | null }) =>
      api.put<OperationDetail>(`/clusters/${id}/connection`, body),
    onSuccess: () => {
      invalidate();
      void client.invalidateQueries({ queryKey: ["clusters", id] });
    },
  });
}

export function useReconnectCluster(id: string) {
  const invalidate = useInvalidateClusters();
  return useMutation({ mutationFn: () => api.post<OperationDetail>(`/clusters/${id}/connect`), onSuccess: invalidate });
}

export function useUnregisterCluster() {
  const invalidate = useInvalidateClusters();
  return useMutation({
    mutationFn: ({ id, confirm }: { id: string; confirm: string }) =>
      apiRequest<void>(`/clusters/${id}/unregister`, { method: "POST", query: { confirm } }),
    onSuccess: invalidate,
  });
}

export function useInstallMetricsServer(id: string) {
  const invalidate = useInvalidateClusters();
  return useMutation({
    mutationFn: (kubeletInsecureTls: boolean) =>
      api.post<OperationDetail>(`/clusters/${id}/addons/metrics-server`, { kubelet_insecure_tls: kubeletInsecureTls }),
    onSuccess: invalidate,
  });
}
