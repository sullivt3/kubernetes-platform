"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { api } from "@/lib/api/client";
import { fetchOperationLogs, queryKeys } from "@/lib/api/hooks";
import type { OperationDetail, OperationLog } from "@/lib/api/types";

export type StreamState = "connecting" | "live" | "polling" | "ended";

const TERMINAL = new Set(["succeeded", "failed", "cancelled"]);
const POLL_MS = 2_000;

/**
 * Live logs and state for one operation. Uses Server-Sent Events (resumable by log
 * id); if streaming fails it falls back to polling the REST endpoints. Changing
 * `generation` (e.g. after a retry) re-subscribes from the last received log.
 */
export function useOperationStream(operationId: string, generation: number) {
  const queryClient = useQueryClient();
  const [logs, setLogs] = useState<OperationLog[]>([]);
  const [state, setState] = useState<StreamState>("connecting");
  const lastId = useRef(0);

  useEffect(() => {
    let disposed = false;
    let ended = false;
    let pollTimer: number | null = null;

    const append = (entries: OperationLog[]) => {
      const fresh = entries.filter((e) => e.id > lastId.current);
      if (!fresh.length) return;
      lastId.current = fresh[fresh.length - 1].id;
      setLogs((prev) => [...prev, ...fresh]);
    };

    const finish = () => {
      ended = true;
      setState("ended");
    };

    const poll = async () => {
      if (disposed) return;
      try {
        append(await fetchOperationLogs(operationId, lastId.current));
        const op = await api.get<OperationDetail>(`/operations/${operationId}`);
        queryClient.setQueryData(queryKeys.operation(operationId), op);
        if (TERMINAL.has(op.status)) {
          append(await fetchOperationLogs(operationId, lastId.current));
          finish();
          return;
        }
      } catch {
        // Transient failure: keep polling. Query errors are surfaced by the page.
      }
      pollTimer = window.setTimeout(poll, POLL_MS);
    };

    const source = new EventSource(`/api/v1/operations/${operationId}/events?after=${lastId.current}`);
    source.addEventListener("open", () => {
      if (!disposed && !ended) setState("live");
    });
    source.addEventListener("log", (event) => {
      append([JSON.parse((event as MessageEvent<string>).data) as OperationLog]);
    });
    source.addEventListener("operation", (event) => {
      const snapshot = JSON.parse((event as MessageEvent<string>).data) as OperationDetail;
      queryClient.setQueryData(queryKeys.operation(operationId), snapshot);
    });
    source.addEventListener("end", () => {
      source.close();
      finish();
    });
    source.onerror = () => {
      source.close();
      if (disposed || ended) return;
      setState("polling");
      if (pollTimer === null) void poll();
    };

    return () => {
      disposed = true;
      source.close();
      if (pollTimer !== null) window.clearTimeout(pollTimer);
    };
  }, [operationId, generation, queryClient]);

  return { logs, state };
}
