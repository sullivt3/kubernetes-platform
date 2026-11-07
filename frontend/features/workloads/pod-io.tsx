"use client";

import "@xterm/xterm/css/xterm.css";

import { Pause, Play, RefreshCw, TerminalSquare } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import { StatusPill } from "@/components/common/status";
import { Button } from "@/components/ui/button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { fetchLogs, logStreamUrl, terminalUrl } from "@/lib/api/workloads";

const MAX_LINES = 5000;

export function ContainerSelect({ containers, value, onChange }: { containers: string[]; value: string; onChange: (v: string) => void }) {
  if (containers.length <= 1) return <span className="font-mono text-xs text-muted-foreground">{value}</span>;
  return (
    <Select value={value} onValueChange={onChange}>
      <SelectTrigger className="h-8 w-48" aria-label="Container"><SelectValue /></SelectTrigger>
      <SelectContent>{containers.map((c) => <SelectItem key={c} value={c}>{c}</SelectItem>)}</SelectContent>
    </Select>
  );
}

/** Pod logs: snapshot (tail / previous / timestamps) or live follow via Server-Sent Events. */
export function PodLogs({ clusterId, namespace, pod, containers }: { clusterId: string; namespace: string; pod: string; containers: string[] }) {
  const [container, setContainer] = useState(containers[0] ?? "");
  const [tail, setTail] = useState(500);
  const [previous, setPrevious] = useState(false);
  const [timestamps, setTimestamps] = useState(false);
  const [follow, setFollow] = useState(false);
  const [lines, setLines] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const box = useRef<HTMLPreElement>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const text = await fetchLogs(clusterId, namespace, pod, { container, tail, previous, timestamps });
      setLines(text ? text.replace(/\n$/, "").split("\n") : []);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [clusterId, namespace, pod, container, tail, previous, timestamps]);

  useEffect(() => {
    if (follow) return;
    const timer = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(timer);
  }, [follow, load]);

  useEffect(() => {
    if (!follow) return;
    const source = new EventSource(logStreamUrl(clusterId, namespace, pod, container, 100));
    source.addEventListener("open", () => setLines([]));
    source.addEventListener("line", (event) => {
      const line = JSON.parse((event as MessageEvent<string>).data) as string;
      setLines((prev) => (prev.length >= MAX_LINES ? [...prev.slice(-MAX_LINES + 1), line] : [...prev, line]));
    });
    source.addEventListener("error", () => {
      setError("Log stream ended or could not be opened.");
      source.close();
      setFollow(false);
    });
    source.addEventListener("end", () => {
      source.close();
      setFollow(false);
    });
    return () => source.close();
  }, [follow, clusterId, namespace, pod, container]);

  useEffect(() => {
    if (box.current) box.current.scrollTop = box.current.scrollHeight;
  }, [lines]);

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-3 text-sm">
        <ContainerSelect containers={containers} value={container} onChange={setContainer} />
        <Select value={String(tail)} onValueChange={(v) => setTail(Number(v))} disabled={follow}>
          <SelectTrigger className="h-8 w-32" aria-label="Lines"><SelectValue /></SelectTrigger>
          <SelectContent>{[100, 500, 2000, 10000].map((n) => <SelectItem key={n} value={String(n)}>last {n}</SelectItem>)}</SelectContent>
        </Select>
        <label className="flex items-center gap-1.5"><input type="checkbox" checked={previous} disabled={follow} onChange={(e) => setPrevious(e.target.checked)} /> previous container</label>
        <label className="flex items-center gap-1.5"><input type="checkbox" checked={timestamps} disabled={follow} onChange={(e) => setTimestamps(e.target.checked)} /> timestamps</label>
        <div className="ml-auto flex gap-2">
          {!follow && <Button variant="outline" size="sm" disabled={loading} onClick={() => void load()}><RefreshCw /> Refresh</Button>}
          <Button size="sm" variant={follow ? "secondary" : "outline"} onClick={() => setFollow((f) => !f)}>
            {follow ? <><Pause /> Stop following</> : <><Play /> Follow</>}
          </Button>
          {follow && <StatusPill tone="success">live</StatusPill>}
        </div>
      </div>
      {error && <p className="text-xs text-amber-300">{error}</p>}
      <pre ref={box} className="h-[420px] overflow-auto rounded-md border border-border bg-black/50 p-3 font-mono text-xs leading-relaxed whitespace-pre-wrap">
        {lines.length ? lines.join("\n") : loading ? "Loading…" : "No log output."}
      </pre>
    </div>
  );
}

/** Interactive shell in a container (xterm.js ↔ platform WebSocket ↔ Kubernetes exec). */
export function PodTerminal({ clusterId, namespace, pod, containers }: { clusterId: string; namespace: string; pod: string; containers: string[] }) {
  const [container, setContainer] = useState(containers[0] ?? "");
  const [session, setSession] = useState(0);
  const [state, setState] = useState<"idle" | "connecting" | "open" | "closed">("idle");
  const host = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (session === 0 || !host.current) return;
    let disposed = false;
    let cleanup = () => {};
    void (async () => {
      const [{ Terminal }, { FitAddon }] = await Promise.all([import("@xterm/xterm"), import("@xterm/addon-fit")]);
      if (disposed || !host.current) return;
      const term = new Terminal({ cursorBlink: true, fontSize: 13, theme: { background: "#0a0a0a" }, convertEol: false });
      const fit = new FitAddon();
      term.loadAddon(fit);
      term.open(host.current);
      fit.fit();
      setState("connecting");
      const ws = new WebSocket(terminalUrl(clusterId, namespace, pod, container));
      const sendResize = () => ws.readyState === WebSocket.OPEN && ws.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
      ws.onopen = () => {
        setState("open");
        sendResize();
        term.focus();
      };
      ws.onmessage = (event) => {
        const message = JSON.parse(event.data as string) as { type: string; data?: string; message?: string; status?: string };
        if (message.type === "output" && message.data) term.write(message.data);
        else if (message.type === "exit") term.write(`\r\n\x1b[33m[session ended: ${message.status}${message.message ? ` – ${message.message}` : ""}]\x1b[0m\r\n`);
        else if (message.type === "error") term.write(`\r\n\x1b[31m${message.message}\x1b[0m\r\n`);
      };
      ws.onclose = (event) => {
        setState("closed");
        if (event.code >= 4000) term.write(`\r\n\x1b[31m[connection refused: ${event.reason || event.code}]\x1b[0m\r\n`);
      };
      const input = term.onData((data) => ws.readyState === WebSocket.OPEN && ws.send(JSON.stringify({ type: "input", data })));
      const resized = term.onResize(sendResize);
      const observer = new ResizeObserver(() => fit.fit());
      observer.observe(host.current);
      cleanup = () => {
        observer.disconnect();
        input.dispose();
        resized.dispose();
        ws.close();
        term.dispose();
      };
    })();
    return () => {
      disposed = true;
      cleanup();
    };
  }, [session, clusterId, namespace, pod, container]);

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-3 text-sm">
        <ContainerSelect containers={containers} value={container} onChange={(v) => { setContainer(v); setSession(0); setState("idle"); }} />
        <Button size="sm" onClick={() => setSession((s) => s + 1)} disabled={state === "connecting"}>
          <TerminalSquare /> {state === "open" ? "Reconnect" : "Open shell"}
        </Button>
        <StatusPill tone={state === "open" ? "success" : state === "connecting" ? "info" : "neutral"}>{state}</StatusPill>
        <span className="ml-auto text-xs text-muted-foreground">Sessions are audited and limited to 1 hour.</span>
      </div>
      <div ref={host} className="h-[420px] rounded-md border border-border bg-[#0a0a0a] p-2" />
      {session === 0 && <p className="text-xs text-muted-foreground">Starts /bin/bash, or /bin/sh when bash is not available.</p>}
    </div>
  );
}
