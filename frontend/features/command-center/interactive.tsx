"use client";

import "@xterm/xterm/css/xterm.css";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { ConfirmDialog } from "@/components/common/confirm-dialog";
import { SearchableChoice } from "@/components/common/searchable-choice";
import { useCan } from "@/components/providers/auth";
import type { PlatformUser } from "@/lib/api/identity";
import { ErrorState } from "@/components/common/page";
import { Button } from "@/components/ui/button";
import { api, buildUrl } from "@/lib/api/client";

type Session = { record_transcript: boolean; transcript_available: boolean; profile: string; target_namespace: string | null; repository_access: boolean; id: string; status: string; namespace: string; expires_at: string; operation_id: string | null; outcome?: string | null };
type Inventory = { repository_policy: { hosts: string[] }; namespaces: string[]; admin: boolean; enabled: boolean; cluster_name: string; lifetime_seconds: number; items: Session[] };

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
  const admin = useCan("platform.manage");
  const [scope, setScope] = useState("");
  const [repositories, setRepositories] = useState(false);
  const [recording, setRecording] = useState(false);
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
      await api.post(base, { confirm: inventory.data?.cluster_name, acknowledge_cluster_admin: admin && acknowledged, repository_access: repositories, record_transcript: recording, acknowledge_transcript: recording, profile: admin ? "admin" : "namespace-readonly", target_namespace: admin ? undefined : scope });
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
  return <section className="space-y-3 rounded border p-4"><h2 className="font-semibold">{admin ? "Interactive sessions · platform administrators" : "Interactive sessions · namespace read-only"}</h2>
    {admin ? <p className="text-sm">A disposable runtime provides kubectl, Helm and a shell with full access to this cluster. Interactive commands bypass per-change reviews and can reveal Secrets. Use the reviewed runner above when you want dry runs and individual confirmations.</p> : <p className="text-sm">Open a read-only shell for an explicitly assigned namespace. Secrets, logs, writes, exec, port forwarding and other namespaces are excluded.</p>}
    <p className="text-sm text-muted-foreground">Maximum 10 minutes, 2 minutes without input, and 2 MiB of output. The runtime can reach the Kubernetes API; repository access is opt-in and limited to approved destinations. Runtime files are temporary. Session lifecycle is audited. Optional terminal output recording requires consent and expires after 24 hours.</p>
    {inventory.isError && <ErrorState error={inventory.error} />}{error !== null && <ErrorState error={error} />}
    {inventory.data && !inventory.data.enabled && <p role="status" className="text-sm">Interactive sessions are disabled. Configure a trusted runtime image on the platform to enable them.</p>}
    {admin && <NamespaceAssignments base={base} clusterId={clusterId} />}
    {admin && inventory.data && <RepositoryDestinations key={inventory.data.repository_policy.hosts.join(",")} base={base} initial={inventory.data.repository_policy.hosts} changed={() => void inventory.refetch()} />}
    {!active && inventory.data?.repository_policy.hosts.length ? <label className="flex gap-2 text-sm"><input type="checkbox" checked={repositories} onChange={(e) => setRepositories(e.target.checked)} />Enable HTTPS repository access: {inventory.data.repository_policy.hosts.join(", ")}</label> : null}
    {!active && inventory.data?.enabled && <label className="flex items-start gap-2 text-sm"><input type="checkbox" checked={recording} onChange={(e) => setRecording(e.target.checked)} />Record terminal output for up to 24 hours. I consent to recording potentially sensitive output, including any echoed commands or passwords. The encrypted recording is available to me and platform administrators; recording failure ends the session.</label>}
    {!active && inventory.data?.enabled && <div className="space-y-3">{!admin && <SearchableChoice label="Assigned namespace" value={scope} options={(inventory.data?.namespaces ?? []).map((value) => ({ value, label: value }))} change={setScope} />}<label className="flex items-start gap-2 text-sm"><input type="checkbox" className="mt-1" checked={acknowledged} disabled={busy} onChange={(e) => setAcknowledged(e.target.checked)} />{admin ? "I acknowledge full cluster access and that changes and Secret reads do not receive individual confirmation." : "I acknowledge read-only access to the selected namespace."}</label><Button disabled={busy || !acknowledged || (!admin && !inventory.data?.namespaces.includes(scope))} onClick={() => setConfirm(true)}>Prepare interactive session</Button></div>}
    {active && <div className="space-y-2 rounded border p-3"><p className="text-sm">Session: {active.status} · {active.profile === "admin" ? "full administrator access" : `read-only: ${active.target_namespace}`} · {active.record_transcript ? "output recording ON" : "recording OFF"} · expires {new Date(active.expires_at).toLocaleTimeString()}</p>{active.operation_id && <Link className="text-sm underline" href={`/operations/${active.operation_id}`}>View runtime preparation</Link>}<div className="flex gap-2">{active.status === "ready" && selected !== active.id && <Button disabled={busy} onClick={() => setSelected(active.id)}>Open interactive terminal</Button>}<Button variant="outline" disabled={busy || active.status === "closing"} onClick={() => void end(active.id)}>End session</Button></div>{active.status === "ready" && <p className="text-xs">Open within 60 seconds; unattached runtimes are removed automatically.</p>}{active.status === "closing" && <p className="text-xs">Cleanup is pending. The platform retries until it can verify grant and namespace removal.</p>}</div>}
    {selected && <LiveTerminal key={selected} clusterId={clusterId} sessionId={selected} />}
    <TranscriptInventory base={base} admin={admin} />
    {inventory.data?.items.filter((row) => row.status === "closed").slice(0, 5).map((row) => <div key={row.id} className="space-y-1 text-xs text-muted-foreground"><p>Closed session {row.id.slice(0, 8)} · {row.outcome ?? "closed"}</p></div>)}
    <ConfirmDialog open={confirm} onOpenChange={setConfirm} title={admin ? "Prepare privileged interactive session" : "Prepare read-only interactive session"} description={admin ? `Grant a short-lived runtime full administrator access to ${inventory.data?.cluster_name}. Commands can change or delete cluster resources and expose Secrets. This runtime is for trusted administrators. Type the cluster name to confirm.` : `Grant a short-lived read-only runtime access to namespace ${scope}. Type the cluster name to confirm.`} confirmText={inventory.data?.cluster_name} confirmLabel="Prepare session" pending={busy} onConfirm={() => void start()} />
  </section>;
}

function NamespaceAssignments({ base, clusterId }: { base: string; clusterId: string }) {
  const [user, setUser] = useState("");
  const [namespace, setNamespace] = useState("");
  const [page, setPage] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [revoke, setRevoke] = useState<string | null>(null);
  const users = useQuery({ queryKey: ["admin", "users", page], queryFn: () => api.get<PlatformUser[]>("/administration/users", { limit: 100, offset: page * 100 }) });
  const namespaces = useQuery({ queryKey: ["command-assignment-namespaces", clusterId], queryFn: () => api.get<{ name: string }[]>(`/clusters/${clusterId}/namespaces`) });
  const assignments = useQuery({ queryKey: ["command-assignments", clusterId], queryFn: () => api.get<{ items: { id: string; user_id: string; namespace: string }[] }>(`${base}/namespace-assignments`) });
  const save = async (id?: string) => {
    setBusy(true); setError(null);
    try { if (id) await api.delete(`${base}/namespace-assignments/${id}`); else await api.post(`${base}/namespace-assignments`, { user_id: user, namespace }); await assignments.refetch(); setRevoke(null); }
    catch (e) { setError(e); } finally { setBusy(false); }
  };
  return <details className="rounded border p-3"><summary>Manage read-only namespace assignments</summary><div className="space-y-3 pt-3">
    <p className="text-sm">Assignments supplement existing operator membership. Revocation ends affected sessions and requests Kubernetes grant cleanup.</p>
    <SearchableChoice label="Platform user" value={user} options={(users.data ?? []).filter((u) => u.is_active).map((u) => ({ value: u.id, label: `${u.display_name} · ${u.email ?? u.id}` }))} change={setUser} />
    <div className="flex gap-2"><Button variant="outline" disabled={!page} onClick={() => setPage(page - 1)}>Previous users</Button><Button variant="outline" disabled={(users.data?.length ?? 0) < 100} onClick={() => setPage(page + 1)}>Next users</Button></div>
    <SearchableChoice label="Application namespace" value={namespace} options={(namespaces.data ?? []).filter((n) => !n.name.startsWith("kube-") && !n.name.startsWith("kp-shell-")).map((n) => ({ value: n.name, label: n.name }))} change={setNamespace} />
    <Button disabled={busy || !user || !namespace} onClick={() => void save()}>Assign read-only access</Button>
    {(error !== null || users.isError || namespaces.isError || assignments.isError) && <ErrorState error={error ?? users.error ?? namespaces.error ?? assignments.error} />}
    {assignments.data?.items.map((g) => <div key={g.id} className="flex items-center justify-between gap-2 text-sm"><span>{users.data?.find((u) => u.id === g.user_id)?.display_name ?? g.user_id} · {g.namespace}</span><Button variant="outline" disabled={busy} onClick={() => setRevoke(g.id)}>Revoke assignment</Button></div>)}
    {assignments.data?.items.length === 0 && <p className="text-sm">No read-only namespace assignments.</p>}
    <ConfirmDialog open={!!revoke} onOpenChange={(open) => !open && setRevoke(null)} title="Revoke namespace assignment" confirmLabel="Revoke assignment" description="This ends affected interactive sessions and removes their temporary Kubernetes permissions." pending={busy} onConfirm={() => revoke && void save(revoke)} />
  </div></details>;
}

function RepositoryDestinations({ base, initial, changed }: { base: string; initial: string[]; changed: () => void }) {
  const [hosts, setHosts] = useState(initial.join("\n"));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [confirm, setConfirm] = useState(false);
  const save = async () => { setBusy(true); setError(null); try { await api.put(`${base}/repository-policy`, { hosts: hosts.split(/\s+/).filter(Boolean) }); setConfirm(false); changed(); } catch (e) { setError(e); } finally { setBusy(false); } };
  return <details className="rounded border p-3"><summary>Manage repository destinations</summary><div className="space-y-2 pt-3"><p className="text-sm">Approve up to 10 public hostnames, one per line. Each runtime pins their resolved public IPs for HTTPS access. Shared IPs may serve other websites. DNS is not opened. Include download, authentication and redirect hosts required by the repository. Changing this policy ends sessions using repository access.</p><textarea aria-label="Approved repository hosts" className="min-h-24 w-full rounded border p-2" value={hosts} onChange={(e) => setHosts(e.target.value)} disabled={busy} /><Button disabled={busy} onClick={() => setConfirm(true)}>Save repository destinations</Button>{error !== null && <ErrorState error={error} />}<ConfirmDialog open={confirm} onOpenChange={setConfirm} title="Update repository access" description="This changes allowed public HTTPS destinations and ends existing sessions that opted into repository access." confirmLabel="Save destinations" pending={busy} onConfirm={() => void save()} /></div></details>;
}

function TranscriptActions({ base, row, changed }: { base: string; row: Session; changed: () => void }) {
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const act = async (remove: boolean) => { setBusy(true); setError(null); try {
    const path = `${base}/${row.id}/transcript`;
    if (remove) { await api.delete(path); setConfirm(false); changed(); }
    else { const data = await api.get<unknown>(path); const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" })); const link = document.createElement("a"); link.href = url; link.download = `session-${row.id}.json`; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); }
  } catch (e) { setError(e); } finally { setBusy(false); } };
  return <div className="space-y-2"><div className="flex gap-2"><Button size="sm" variant="outline" disabled={busy} onClick={() => void act(false)}>Download transcript</Button><Button size="sm" variant="outline" onClick={() => setConfirm(true)} disabled={busy || row.status !== "closed"}>Delete transcript</Button></div>{error !== null && <ErrorState error={error} />}<ConfirmDialog open={confirm} onOpenChange={setConfirm} title="Delete transcript" description="This permanently deletes the platform's stored recording. Previously downloaded copies and backups are separate." confirmLabel="Delete transcript" pending={busy} onConfirm={() => void act(true)} /></div>;
}

function TranscriptInventory({ base, admin }: { base: string; admin: boolean }) {
  const [page, setPage] = useState(0);
  const [allUsers, setAllUsers] = useState(false);
  const records = useQuery({ queryKey: ["command-transcript-metadata", base, page, allUsers], queryFn: () => api.get<{ items: (Session & { owner: string })[]; has_more: boolean }>(`${base}/transcripts`, { offset: page * 20, all_users: allUsers }), refetchInterval: 5000 });
  return <details className="rounded border p-3"><summary>Recorded terminal output</summary><div className="space-y-3 pt-3">{admin && <label className="flex gap-2 text-sm"><input type="checkbox" checked={allUsers} onChange={(e) => { setAllUsers(e.target.checked); setPage(0); }} />Include recordings from other users</label>}{records.isPending && <p>Loading recordings…</p>}{records.isError && <ErrorState error={records.error} />}{records.data?.items.length === 0 && <p className="text-sm">No retained recordings.</p>}{records.data?.items.map((row) => <div key={row.id} className="space-y-2 rounded border p-2 text-sm"><p>{row.id.slice(0, 8)} · {row.status}{allUsers ? ` · ${row.owner}` : ""}</p><p className="text-xs">Downloads are audited. Recordings expire 24 hours after session creation. Active recordings may be incomplete.</p><TranscriptActions base={base} row={row} changed={() => void records.refetch()} /></div>)}<div className="flex gap-2"><Button variant="outline" disabled={!page} onClick={() => setPage(page - 1)}>Previous recordings</Button><Button variant="outline" disabled={!records.data?.has_more} onClick={() => setPage(page + 1)}>Next recordings</Button></div></div></details>;
}
