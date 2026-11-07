"use client";

// Node management: cordon, labels, taints, drain, system logs, add and remove nodes.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, apiRequest } from "./client";
import { queryKeys } from "./hooks";
import type { MachineTemplateInput, OperationDetail, Taint } from "./types";

function useRefresh(clusterId: string) {
  const client = useQueryClient();
  return () => {
    void client.invalidateQueries({ queryKey: ["clusters", clusterId] });
    void client.invalidateQueries({ queryKey: queryKeys.clusters });
    void client.invalidateQueries({ queryKey: queryKeys.machines });
    void client.invalidateQueries({ queryKey: queryKeys.operations });
  };
}

const nodePath = (clusterId: string, name: string) => `/clusters/${clusterId}/nodes/${encodeURIComponent(name)}`;

export function useCordonNode(clusterId: string) {
  const refresh = useRefresh(clusterId);
  return useMutation({
    mutationFn: ({ name, cordon }: { name: string; cordon: boolean }) =>
      api.post<unknown>(`${nodePath(clusterId, name)}/${cordon ? "cordon" : "uncordon"}`, {}),
    onSuccess: refresh,
  });
}

export function useUpdateNodeLabels(clusterId: string) {
  const refresh = useRefresh(clusterId);
  return useMutation({
    mutationFn: ({ name, set, remove }: { name: string; set: Record<string, string>; remove: string[] }) =>
      apiRequest<Record<string, string>>(`${nodePath(clusterId, name)}/labels`, { method: "PATCH", body: { set, remove } }),
    onSuccess: refresh,
  });
}

export function useReplaceNodeTaints(clusterId: string) {
  const refresh = useRefresh(clusterId);
  return useMutation({
    mutationFn: ({ name, taints }: { name: string; taints: Taint[] }) =>
      api.put<unknown>(`${nodePath(clusterId, name)}/taints`, { taints }),
    onSuccess: refresh,
  });
}

export interface DrainInput {
  force: boolean;
  delete_emptydir_data: boolean;
  grace_period_seconds?: number | null;
  timeout_seconds: number;
}

export function useDrainNode(clusterId: string) {
  const refresh = useRefresh(clusterId);
  return useMutation({
    mutationFn: ({ name, ...options }: DrainInput & { name: string }) =>
      api.post<OperationDetail>(`${nodePath(clusterId, name)}/drain`, options),
    onSuccess: refresh,
  });
}

export function useNodeLogs(clusterId: string, name: string, unit: "kubelet" | "containerd", enabled: boolean) {
  return useQuery({
    queryKey: ["clusters", clusterId, "node-logs", name, unit],
    queryFn: () => apiRequest<string>(`${nodePath(clusterId, name)}/logs`, { query: { unit, lines: 400 }, responseType: "text" }),
    enabled,
    retry: false,
  });
}

export interface AddNodesInput {
  pool: string;
  existing_machine_ids: string[];
  provision_count: number;
  machine_template?: MachineTemplateInput | null;
  provider_connection_id?: string | null;
  machine_credential_id?: string | null;
  labels?: Record<string, string>;
  taints?: Taint[];
}

export function useAddNodes(clusterId: string) {
  const refresh = useRefresh(clusterId);
  return useMutation({
    mutationFn: (body: AddNodesInput) => api.post<OperationDetail>(`/clusters/${clusterId}/nodes`, body),
    onSuccess: refresh,
  });
}

export function useRemoveNode(clusterId: string) {
  const refresh = useRefresh(clusterId);
  return useMutation({
    mutationFn: ({ name, destroyMachine, force }: { name: string; destroyMachine: boolean; force: boolean }) =>
      api.delete<OperationDetail>(nodePath(clusterId, name), {
        confirm: name,
        destroy_machine: destroyMachine || undefined,
        force: force || undefined,
      }),
    onSuccess: refresh,
  });
}
