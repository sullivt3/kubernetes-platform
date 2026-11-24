"use client";

import "@xterm/xterm/css/xterm.css";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { ConfirmDialog } from "@/components/common/confirm-dialog";
import { ErrorState } from "@/components/common/page";
import { Button } from "@/components/ui/button";
import { api, buildUrl } from "@/lib/api/client";

type Session = { id: string; status: string; namespace: string; expires_at: string; operation_id: string | null; outcome?: string | null };
type Inventory = { enabled: boolean; cluster_name: string; lifetime_seconds: number; items: Session[] };

function LiveTerminal({ clusterId, sessionId }: { clusterId: string; sessionId: string }) {
  const host = useRef<HTMLDivElement>(null);
  const [state, setState] = useState("connecting");
  useEffect(() => {
    let disposed = false;
    let cleanup = () => {};
    void (async () => {
      const [{ Terminal }, { FitAddon }] = await Promise.all([import("@xterm/xterm"), import("@xterm/addon-fit")]);
      if (disposed || !host.current) return;
      const terminal = new Terminal({ cursorBlink: true, fontSize: 13, scrollback: 2000, theme: { background: "#0a0a0a" } });
      const fit = new FitAddon(); terminal.loadAddon(fit); terminal.open(host.current); fit.fit();
      const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
      const ws = new WebSocket(`${scheme}//${window.location.host}${buildUrl(`/clusters/${clusterId}/command-sessions/${sessionId}/attach`)}`);
      const sendResize = () => { if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "resize", cols: Math.min(500, terminal.cols), rows: Math.min(500, terminal.rows) })); };
      ws.onopen = () => { if (!disposed) setState("open"); sendResize(); terminal.focus(); };
      ws.onmessage = (event) => {
        const message = JSON.parse(event.data as string) as { type: string; data?: string; message?: string; status?: string };
        if (message.type === "output" && message.data) terminal.write(message.data);
        else if (message.type === "exit") terminal.write(`\r\n[session ended: ${message.status ?? "closed"}]\r\n`);
        else if (message.type === "error") terminal.write(`\r\n${message.message ?? "Terminal connection failed"}\r\n`);
      };
      ws.onclose = () => { if (!disposed) setState("closed"); terminal.write("\r\n[session closed; cleanup requested]\r\n"); };
      ws.onerror = () => { if (!disposed) setState("connection failed"); };
      const input = terminal.onData((text) => {
        const points = Array.from(text);
        for (let i = 0; i < points.length; i += 1024) {
          if (ws.readyState !== WebSocket.OPEN || ws.bufferedAmount > 65536) break;
          ws.send(JSON.stringify({ type: "input", data: points.slice(i, i + 1024).join("") }));
        }
      });
      const resize = terminal.onResize(sendResize);
      const observer = new ResizeObserver(() => fit.fit()); observer.observe(host.current);
      cleanup = () => { observer.disconnect(); input.dispose(); resize.dispose(); ws.close(); terminal.dispose(); };
    })();
    return () => { disposed = true; cleanup(); };
  }, [clusterId, sessionId]);
  return <section aria-label="Interactive cluster terminal" className="space-y-2"><p role="status" className="text-sm">Interactive session: {state}</p><div ref={host} className="h-[420px] rounded border bg-[#0a0a0a] p-2" /><p className="text-xs text-muted-foreground">Ctrl+C interrupts the foreground command. End session revokes the grant and removes the runtime. Sessions are single-use; reconnect by creating a new session.</p></section>;
}

export function InteractiveSessions({ clusterId }: { clusterId: string }) {
  const base = `/clusters/${clusterId}/command-sessions`;
  const [acknowledged, setAcknowledged] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const epoch = useRef(0); useEffect(() => () => { epoch.current += 1; }, []);
  const inventory = useQuery({ queryKey: ["command-sessions", clusterId], queryFn: () => api.get<Inventory>(base), refetchInterval: 2000 });
  const active = inventory.data?.items.find((row) => row.status !== "closed");
  const start = async () => {
    const request = epoch.current; setBusy(true); setError(null);
    try {
      await api.post(base, { confirm: inventory.data?.cluster_name, acknowledge_cluster_admin: acknowledged });
      if (request === epoch.current) { setConfirm(false); setAcknowledged(false); void inventory.refetch(); }
    } catch (e) { if (request === epoch.current) setError(e); }
    finally { if (request === epoch.current) setBusy(false); }
  };
  const end = async (id: string) => {
    const request = epoch.current; setBusy(true); setError(null);
    try { await api.post(`${base}/${id}/close`); if (request === epoch.current) { setSelected(null); void inventory.refetch(); } }
    catch (e) { if (request === epoch.current) setError(e); }
    finally { if (request === epoch.current) setBusy(false); }
  };
  return <section className="space-y-3 rounded border p-4"><h2 className="font-semibold">Interactive sessions · platform administrators</h2>
    <p className="text-sm">A disposable runtime provides kubectl, Helm and a shell with full access to this cluster. Interactive commands bypass per-change reviews and can reveal Secrets. Use the reviewed runner above when you want dry runs and individual confirmations.</p>
    <p className="text-sm text-muted-foreground">Maximum 10 minutes, 2 minutes without input, and 2 MiB of output. The runtime can reach the Kubernetes API; external chart repositories are unavailable. Runtime files are temporary. Session lifecycle is audited; command text and output are not recorded.</p>
    {inventory.isError && <ErrorState error={inventory.error} />}{error !== null && <ErrorState error={error} />}
    {inventory.data && !inventory.data.enabled && <p role="status" className="text-sm">Interactive sessions are disabled. Configure a trusted runtime image on the platform to enable them.</p>}
    {!active && inventory.data?.enabled && <div className="space-y-3"><label className="flex items-start gap-2 text-sm"><input type="checkbox" className="mt-1" checked={acknowledged} disabled={busy} onChange={(e) => setAcknowledged(e.target.checked)} />I acknowledge full cluster access and that changes and Secret reads do not receive individual confirmation.</label><Button disabled={busy || !acknowledged} onClick={() => setConfirm(true)}>Prepare interactive session</Button></div>}
    {active && <div className="space-y-2 rounded border p-3"><p className="text-sm">Session: {active.status} · expires {new Date(active.expires_at).toLocaleTimeString()}</p>{active.operation_id && <Link className="text-sm underline" href={`/operations/${active.operation_id}`}>View runtime preparation</Link>}<div className="flex gap-2">{active.status === "ready" && selected !== active.id && <Button disabled={busy} onClick={() => setSelected(active.id)}>Open interactive terminal</Button>}<Button variant="outline" disabled={busy || active.status === "closing"} onClick={() => void end(active.id)}>End session</Button></div>{active.status === "ready" && <p className="text-xs">Open within 60 seconds; unattached runtimes are removed automatically.</p>}{active.status === "closing" && <p className="text-xs">Cleanup is pending. The platform retries until it can verify grant and namespace removal.</p>}</div>}
    {selected && <LiveTerminal key={selected} clusterId={clusterId} sessionId={selected} />}
    {inventory.data?.items.filter((row) => row.status === "closed").slice(0, 5).map((row) => <p key={row.id} className="text-xs text-muted-foreground">Closed session {row.id.slice(0, 8)} · {row.outcome ?? "closed"}</p>)}
    <ConfirmDialog open={confirm} onOpenChange={setConfirm} title="Prepare privileged interactive session" description={`Grant a short-lived runtime full administrator access to ${inventory.data?.cluster_name}. Commands can change or delete cluster resources and expose Secrets. This runtime is for trusted administrators. Type the cluster name to confirm.`} confirmText={inventory.data?.cluster_name} confirmLabel="Prepare session" pending={busy} onConfirm={() => void start()} />
  </section>;
}
