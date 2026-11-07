"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { toast } from "sonner";

export type LiveMode = "connecting" | "live" | "polling";

const LiveUpdatesContext = createContext<{ mode: LiveMode }>({ mode: "connecting" });

export function useLiveUpdates() {
  return useContext(LiveUpdatesContext);
}

interface ChangeEvent {
  operation_id: string;
  kind: "status" | "step" | "log" | "progress";
  status: string | null;
}

const TERMINAL: Record<string, "success" | "error" | "warning"> = {
  succeeded: "success",
  failed: "error",
  cancelled: "warning",
};
const RECONNECT_MS = 30_000;

/**
 * Subscribes to the organization's operation change feed (SSE). Changes invalidate
 * cached queries; when the feed is unavailable the app falls back to polling.
 */
export function LiveUpdatesProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const router = useRouter();
  const [mode, setMode] = useState<LiveMode>("connecting");
  const pending = useRef<number | null>(null);

  useEffect(() => {
    let source: EventSource | null = null;
    let reconnectTimer: number | null = null;
    let disposed = false;

    const scheduleInvalidate = () => {
      if (pending.current !== null) return;
      pending.current = window.setTimeout(() => {
        pending.current = null;
        void queryClient.invalidateQueries({ queryKey: ["operations", "list"] });
      }, 400);
    };

    const connect = () => {
      if (disposed) return;
      source = new EventSource("/api/v1/operations/stream/changes");
      source.addEventListener("ready", () => setMode("live"));
      source.addEventListener("change", (event) => {
        let change: ChangeEvent;
        try {
          change = JSON.parse((event as MessageEvent<string>).data) as ChangeEvent;
        } catch {
          return;
        }
        if (change.kind === "log") return; // logs are streamed by the detail view itself
        scheduleInvalidate();
        void queryClient.invalidateQueries({ queryKey: ["operations", "detail", change.operation_id] });
        const tone = change.kind === "status" && change.status ? TERMINAL[change.status] : undefined;
        if (tone) {
          toast[tone](`Operation ${change.status}`, {
            description: `Operation ${change.operation_id.slice(0, 8)}`,
            action: { label: "View", onClick: () => router.push(`/operations/${change.operation_id}`) },
          });
        }
      });
      source.addEventListener("unavailable", () => {
        setMode("polling");
        source?.close();
        reconnectTimer = window.setTimeout(connect, RECONNECT_MS);
      });
      source.onerror = () => {
        // EventSource retries automatically; poll in the meantime.
        setMode("polling");
      };
    };

    connect();
    return () => {
      disposed = true;
      source?.close();
      if (reconnectTimer !== null) window.clearTimeout(reconnectTimer);
      if (pending.current !== null) window.clearTimeout(pending.current);
    };
  }, [queryClient, router]);

  return <LiveUpdatesContext.Provider value={{ mode }}>{children}</LiveUpdatesContext.Provider>;
}
