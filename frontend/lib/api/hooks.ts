"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { useLiveUpdates } from "@/components/providers/live-updates";
import { api, apiRequest } from "./client";
import type {
  AuditEvent,
  ClusterDetail,
  ClusterSpecInput,
  ClusterSummary,
  ClusterValidation,
  KubernetesVersions,
  Machine,
  MachineRegister,
  Credential,
  CredentialCreate,
  DistributionDescriptor,
  OperationDetail,
  OperationLog,
  OperationStatus,
  OperationSummary,
  Page,
  ProviderDescriptor,
  ReadinessReport,
  SystemInfo,
} from "./types";

export const queryKeys = {
  readiness: ["health", "ready"] as const,
  systemInfo: ["system", "info"] as const,
  providers: ["catalog", "providers"] as const,
  distributions: ["catalog", "distributions"] as const,
  operations: ["operations"] as const,
  operationList: (filters: OperationFilters) => ["operations", "list", filters] as const,
  operation: (id: string) => ["operations", "detail", id] as const,
  operationLogs: (id: string) => ["operations", "logs", id] as const,
  credentials: ["credentials"] as const,
  audit: (filters: Record<string, unknown>) => ["audit", filters] as const,
  clusters: ["clusters"] as const,
  cluster: (id: string) => ["clusters", id] as const,
  machines: ["machines"] as const,
  kubernetesVersions: ["catalog", "kubernetes-versions"] as const,
};

// ---- Platform -----------------------------------------------------------------

export function useReadiness() {
  return useQuery({
    queryKey: queryKeys.readiness,
    // 503 still carries a full report; surface it rather than treating it as an error.
    queryFn: async () => {
      const response = await fetch("/api/v1/health/ready", { headers: { Accept: "application/json" } });
      if (response.status !== 200 && response.status !== 503) {
        throw new Error("The platform API is unreachable.");
      }
      return (await response.json()) as ReadinessReport;
    },
    refetchInterval: 15_000,
    retry: false,
  });
}

export function useSystemInfo() {
  return useQuery({
    queryKey: queryKeys.systemInfo,
    queryFn: () => api.get<SystemInfo>("/system/info"),
    staleTime: 60_000,
  });
}

export function useProviders() {
  return useQuery({
    queryKey: queryKeys.providers,
    queryFn: () => api.get<ProviderDescriptor[]>("/catalog/providers"),
    staleTime: 5 * 60_000,
  });
}

export function useDistributions() {
  return useQuery({
    queryKey: queryKeys.distributions,
    queryFn: () => api.get<DistributionDescriptor[]>("/catalog/distributions"),
    staleTime: 5 * 60_000,
  });
}

// ---- Operations ---------------------------------------------------------------

export interface OperationFilters {
  status?: OperationStatus[];
  active?: boolean;
  type?: string;
  limit?: number;
  offset?: number;
}

/** Poll only when live push updates are unavailable. */
function usePollingInterval(ms: number): number | false {
  const { mode } = useLiveUpdates();
  return mode === "live" ? false : ms;
}

export function useOperations(filters: OperationFilters = {}) {
  const refetchInterval = usePollingInterval(5_000);
  return useQuery({
    queryKey: queryKeys.operationList(filters),
    queryFn: () =>
      api.get<Page<OperationSummary>>("/operations", {
        status: filters.status,
        active: filters.active || undefined,
        type: filters.type,
        limit: filters.limit ?? 50,
        offset: filters.offset ?? 0,
      }),
    refetchInterval,
  });
}

export function useOperation(id: string) {
  return useQuery({
    queryKey: queryKeys.operation(id),
    queryFn: () => api.get<OperationDetail>(`/operations/${id}`),
  });
}

export function fetchOperationLogs(id: string, after: number) {
  return api.get<OperationLog[]>(`/operations/${id}/logs`, { after, limit: 2000 });
}

function useInvalidateOperations() {
  const client = useQueryClient();
  return (operation?: OperationDetail) => {
    if (operation) client.setQueryData(queryKeys.operation(operation.id), operation);
    return client.invalidateQueries({ queryKey: queryKeys.operations });
  };
}

export function useStartSelfCheck() {
  const invalidate = useInvalidateOperations();
  return useMutation({
    mutationFn: () => api.post<OperationDetail>("/system/self-check"),
    onSuccess: (operation) => invalidate(operation),
  });
}

export function useCancelOperation() {
  const invalidate = useInvalidateOperations();
  return useMutation({
    mutationFn: (id: string) => api.post<OperationDetail>(`/operations/${id}/cancel`),
    onSuccess: (operation) => invalidate(operation),
  });
}

export function useRetryOperation() {
  const invalidate = useInvalidateOperations();
  return useMutation({
    mutationFn: (id: string) => api.post<OperationDetail>(`/operations/${id}/retry`),
    onSuccess: (operation) => invalidate(operation),
  });
}

// ---- Clusters -----------------------------------------------------------------

export function useClusters() {
  const refetchInterval = usePollingInterval(10_000);
  return useQuery({
    queryKey: queryKeys.clusters,
    queryFn: () => api.get<ClusterSummary[]>("/clusters"),
    refetchInterval,
  });
}

export function useCluster(id: string) {
  const refetchInterval = usePollingInterval(5_000);
  return useQuery({
    queryKey: queryKeys.cluster(id),
    queryFn: () => api.get<ClusterDetail>(`/clusters/${id}`),
    refetchInterval,
  });
}

export function useKubernetesVersions() {
  return useQuery({
    queryKey: queryKeys.kubernetesVersions,
    queryFn: () => api.get<KubernetesVersions>("/catalog/kubernetes-versions"),
    staleTime: 30 * 60_000,
  });
}

export function useValidateCluster() {
  return useMutation({
    mutationFn: (spec: ClusterSpecInput) => api.post<ClusterValidation>("/clusters/validate", { spec }),
  });
}

export function useCreateCluster() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: { spec: ClusterSpecInput; description?: string }) =>
      api.post<{ cluster: ClusterSummary; operation: OperationDetail }>("/clusters", body),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: queryKeys.clusters });
      void client.invalidateQueries({ queryKey: queryKeys.machines });
      void client.invalidateQueries({ queryKey: queryKeys.operations });
    },
  });
}

export function useDeleteCluster() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({
      id,
      confirm,
      force,
      destroyMachines,
    }: {
      id: string;
      confirm: string;
      force: boolean;
      destroyMachines?: boolean;
    }) =>
      api.delete<OperationDetail>(`/clusters/${id}`, {
        confirm,
        force: force || undefined,
        destroy_machines: destroyMachines || undefined,
      }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: queryKeys.clusters });
      void client.invalidateQueries({ queryKey: queryKeys.operations });
    },
  });
}

export function useUpgradeCluster() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, targetVersion, allowEmptydirData, targetReleaseRevision }: { id: string; targetVersion: string; allowEmptydirData: boolean; targetReleaseRevision?: number }) =>
      api.post<OperationDetail>(`/clusters/${id}/upgrade`, {
        target_version: targetVersion,
        allow_emptydir_data: allowEmptydirData,
        target_release_revision: targetReleaseRevision,
      }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: queryKeys.clusters });
      void client.invalidateQueries({ queryKey: queryKeys.operations });
    },
  });
}

// ---- Machines -----------------------------------------------------------------

export function useMachines() {
  const refetchInterval = usePollingInterval(10_000);
  return useQuery({
    queryKey: queryKeys.machines,
    queryFn: () => api.get<Machine[]>("/machines"),
    refetchInterval,
  });
}

export function useRegisterMachine() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: MachineRegister) =>
      api.post<{ machine: Machine; operation: OperationDetail }>("/machines", body),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: queryKeys.machines });
      void client.invalidateQueries({ queryKey: queryKeys.operations });
    },
  });
}

export function useInspectMachine() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, acceptNewHostKey, confirm }: { id: string; acceptNewHostKey?: boolean; confirm?: string }) =>
      apiRequest<OperationDetail>(`/machines/${id}/inspect`, {
        method: "POST",
        query: { confirm },
        body: { accept_new_host_key: Boolean(acceptNewHostKey) },
      }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: queryKeys.machines });
      void client.invalidateQueries({ queryKey: queryKeys.operations });
    },
  });
}

export function useDeleteMachine() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, confirm }: { id: string; confirm: string }) => api.delete<void>(`/machines/${id}`, { confirm }),
    onSuccess: () => client.invalidateQueries({ queryKey: queryKeys.machines }),
  });
}

// ---- Credentials --------------------------------------------------------------

export function useCredentials() {
  return useQuery({
    queryKey: queryKeys.credentials,
    queryFn: () => api.get<Page<Credential>>("/credentials", { limit: 500 }),
  });
}

export function useCreateCredential() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: CredentialCreate) => api.post<Credential>("/credentials", body),
    onSuccess: () => client.invalidateQueries({ queryKey: queryKeys.credentials }),
  });
}

export function useDeleteCredential() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, confirm }: { id: string; confirm: string }) =>
      api.delete<void>(`/credentials/${id}`, { confirm }),
    onSuccess: () => client.invalidateQueries({ queryKey: queryKeys.credentials }),
  });
}

// ---- Audit --------------------------------------------------------------------

export function useAuditEvents(filters: { resource_type?: string; limit: number; offset: number }) {
  return useQuery({
    queryKey: queryKeys.audit(filters),
    queryFn: () => api.get<Page<AuditEvent>>("/audit-events", filters),
    placeholderData: (previous) => previous,
  });
}
