"use client";

// Provider connections, provisioning, power/destroy and network discovery.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, apiRequest } from "./client";
import { queryKeys } from "./hooks";
import type { Machine, OperationDetail, ProviderConnection } from "./types";

export const infraKeys = {
  connections: ["provider-connections"] as const,
  connection: (id: string) => ["provider-connections", id] as const,
};

export interface ConnectionInput {
  name: string;
  provider_type: string;
  config: Record<string, unknown>;
  credential_id: string | null;
}

export interface ProvisionInput {
  names: string[];
  cpu_cores: number;
  memory_mb: number;
  disk_gb: number;
  credential_id: string;
  ssh_username?: string;
  network?: string | null;
  image?: string | null;
  labels?: Record<string, string>;
  provider_options?: Record<string, unknown>;
}

export interface ImportInput {
  external_id: string;
  name: string;
  host: string;
  port: number;
  username?: string | null;
  credential_id: string;
  kube_api_host?: string | null;
  kube_api_port?: number | null;
}

export type PowerAction = "start" | "stop" | "reboot";

function useInvalidate() {
  const client = useQueryClient();
  return () => {
    void client.invalidateQueries({ queryKey: infraKeys.connections });
    void client.invalidateQueries({ queryKey: queryKeys.machines });
    void client.invalidateQueries({ queryKey: queryKeys.operations });
  };
}

export function useConnections() {
  return useQuery({
    queryKey: infraKeys.connections,
    queryFn: () => api.get<ProviderConnection[]>("/provider-connections"),
    refetchInterval: (query) =>
      query.state.data?.some((c) => c.status === "unknown") ? 3_000 : false,
  });
}

export function useCreateConnection() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: (body: ConnectionInput) =>
      api.post<{ connection: ProviderConnection; operation: OperationDetail }>("/provider-connections", body),
    onSuccess: invalidate,
  });
}

export function useUpdateConnection() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: ({ id, ...body }: { id: string; config?: Record<string, unknown>; credential_id?: string | null }) =>
      apiRequest<{ connection: ProviderConnection; operation: OperationDetail }>(`/provider-connections/${id}`, {
        method: "PUT",
        body,
      }),
    onSuccess: invalidate,
  });
}

export function useDeleteConnection() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: ({ id, confirm }: { id: string; confirm: string }) =>
      api.delete<void>(`/provider-connections/${id}`, { confirm }),
    onSuccess: invalidate,
  });
}

export function useValidateConnection() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: (id: string) => api.post<OperationDetail>(`/provider-connections/${id}/validate`, {}),
    onSuccess: invalidate,
  });
}

export function useDiscoverMachines() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: (id: string) => api.post<OperationDetail>(`/provider-connections/${id}/discover`, {}),
    onSuccess: invalidate,
  });
}

export function useProvisionMachines() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: ({ connectionId, ...body }: ProvisionInput & { connectionId: string }) =>
      api.post<{ machine: Machine; operation: OperationDetail }[]>(
        `/provider-connections/${connectionId}/machines`,
        body,
      ),
    onSuccess: invalidate,
  });
}

export function useImportMachine() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: ({ connectionId, ...body }: ImportInput & { connectionId: string }) =>
      api.post<{ machine: Machine; operation: OperationDetail }>(`/provider-connections/${connectionId}/import`, body),
    onSuccess: invalidate,
  });
}

export function usePowerMachine() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: ({ id, action, confirm }: { id: string; action: PowerAction; confirm?: string }) =>
      apiRequest<OperationDetail>(`/machines/${id}/power`, { method: "POST", query: { confirm }, body: { action } }),
    onSuccess: invalidate,
  });
}

export function useDestroyMachine() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: ({ id, confirm }: { id: string; confirm: string }) =>
      apiRequest<OperationDetail>(`/machines/${id}/destroy`, { method: "POST", query: { confirm } }),
    onSuccess: invalidate,
  });
}

export function useNetworkScan() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: (body: { ranges: string[]; ports: number[]; timeout_seconds?: number }) =>
      api.post<OperationDetail>("/network-discovery", body),
    onSuccess: invalidate,
  });
}

/** Parse "node-{1..3}" style input or a comma list into machine names. */
export function expandNames(prefix: string, count: number, start = 1): string[] {
  return Array.from({ length: count }, (_, i) => `${prefix}-${start + i}`);
}

const TERMINAL = new Set(["succeeded", "failed", "cancelled"]);

/** An operation, polled until it reaches a terminal status (for results such as discovery). */
export function useOperationResult(id: string | null | undefined) {
  return useQuery({
    queryKey: ["operations", "detail", id ?? "none"],
    queryFn: () => api.get<OperationDetail>(`/operations/${id}`),
    enabled: Boolean(id),
    refetchInterval: (query) => (query.state.data && TERMINAL.has(query.state.data.status) ? false : 2_000),
  });
}
