"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, apiRequest } from "./client";
import type { OperationDetail } from "./types";

export interface DnsLookupInput {
  namespace: string;
  hostname: string;
  record_type: "A" | "AAAA" | "CNAME" | "SRV" | "TXT" | "MX";
}

export interface NfsInstallInput {
  storage_class: string | null;
  server: string | null;
  share: string | null;
  make_default: boolean;
}

export interface NfsStatus {
  driver: string;
  version: string;
  installed: boolean;
  managed: boolean;
  ready: boolean;
  controller_ready: boolean;
  nodes_ready: number;
  nodes_desired: number;
  storage_classes: { name: string; managed: boolean }[];
}

export function useDnsLookup(clusterId: string) {
  return useMutation({ mutationFn: (body: DnsLookupInput) => api.post<OperationDetail>(`/clusters/${clusterId}/dns/lookup`, body) });
}

export function useNfsStatus(clusterId: string) {
  return useQuery({
    queryKey: ["nfs-csi", clusterId],
    queryFn: () => api.get<NfsStatus>(`/clusters/${clusterId}/storage/nfs`),
    refetchInterval: 10_000,
    retry: false,
  });
}

export function useNfsAction(clusterId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (action: { install: NfsInstallInput } | { remove: string }) => "install" in action
      ? api.post<OperationDetail>(`/clusters/${clusterId}/storage/nfs`, action.install)
      : apiRequest<OperationDetail>(`/clusters/${clusterId}/storage/nfs`, { method: "DELETE", query: { confirm: action.remove } }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["nfs-csi", clusterId] });
      void client.invalidateQueries({ queryKey: ["operations"] });
    },
  });
}

export function usePhase6Operation(id: string | null) {
  return useQuery({
    queryKey: ["operations", "detail", id],
    queryFn: () => api.get<OperationDetail>(`/operations/${id}`),
    enabled: Boolean(id),
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status && ["succeeded", "failed", "cancelled"].includes(status) ? false : 2000;
    },
  });
}
