"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";
import { EmptyState, ErrorState, PageHeader, TableSkeleton } from "@/components/common/page";
import { useCan } from "@/components/providers/auth";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ClusterScope, LiveError } from "@/features/kubernetes/shared";
import { api, ApiError, buildUrl } from "@/lib/api/client";
import { useClusters } from "@/lib/api/hooks";
import { formatBytes, formatDateTime } from "@/lib/format";

type Archive = { id: string; created_at: string; bytes: number };
type Platform = { items: Archive[]; can_backup: boolean; can_restore: boolean };
type Job = { name: string; phase: string; created_at: string; backup: string | null; location: string | null; errors: number; warnings: number };
type Inventory = { backups: Job[]; restores: Job[]; locations: { name: string; phase: string; writable: boolean }[]; truncated: string[] };
const selectClass = "rounded-md border bg-background px-2 py-1.5 text-sm";

function PlatformBackups() {
  const [selected, setSelected] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const query = useQuery({ queryKey: ["platform-backups"], queryFn: () => api.get<Platform>("/backups"), refetchInterval: 10000 });
  const mutation = useMutation({
    mutationFn: (restore: boolean) => restore
      ? api.post<{ id: string }>(`/backups/${selected}/restore`, { confirm: confirmation })
      : api.post<{ id: string }>("/backups"),
    onSuccess: () => { setSelected(""); setConfirmation(""); void query.refetch(); },
  });
  if (query.isError) return <ErrorState error={query.error} onRetry={() => query.refetch()} />;
  if (!query.data) return <TableSkeleton />;
  const data = query.data;
  return <section className="space-y-4">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <p className="text-sm text-muted-foreground">Encrypted PostgreSQL archives include the platform credential keyring.</p>
      <Button disabled={!data.can_backup || mutation.isPending} onClick={() => mutation.mutate(false)}>Create platform backup</Button>
    </div>
    {!data.can_backup && <p role="status">Configure BACKUP_DIRECTORY and BACKUP_RECIPIENT on the API and worker to enable backups.</p>}
    {mutation.isError && <ErrorState error={mutation.error} />}
    {mutation.data && <Link href={`/operations/${mutation.data.id}`} className="underline">View backup or recovery operation</Link>}
    {data.items.length === 0 ? <EmptyState title="No platform archives" /> : (
      <Table><TableHeader><TableRow><TableHead>Archive</TableHead><TableHead>Created</TableHead><TableHead>Size</TableHead><TableHead>Actions</TableHead></TableRow></TableHeader>
        <TableBody>{data.items.map((a) => <TableRow key={a.id}><TableCell className="font-mono">{a.id}</TableCell><TableCell>{formatDateTime(a.created_at)}</TableCell>
          <TableCell>{formatBytes(a.bytes)}</TableCell><TableCell className="space-x-3">
            <a className="underline" href={buildUrl(`/backups/${a.id}/archive`)} download>Download</a>
            {data.can_restore && <Button variant="outline" size="sm" onClick={() => { setSelected(a.id); setConfirmation(""); }}>Restore copy</Button>}
          </TableCell></TableRow>)}</TableBody></Table>)}
    {!data.can_restore && data.items.length > 0 && <p className="text-sm">Use the host recovery command with your age identity, or configure a worker recovery identity and private key output directory to restore here.</p>}
    {selected && <form className="space-y-2 rounded-lg border p-4" onSubmit={(e) => {e.preventDefault(); mutation.mutate(true);}}>
      <p>Restore archive <code>{selected}</code> to a new recovery database. Enter its archive ID to confirm.</p>
      <Input aria-label="Confirm archive ID" value={confirmation} onChange={(e) => setConfirmation(e.target.value)} />
      <Button disabled={confirmation !== selected || mutation.isPending}>Restore recovery copy</Button>
      <Button type="button" variant="ghost" onClick={() => setSelected("")}>Cancel</Button>
    </form>}
  </section>;
}

function ClusterBackups({ id }: { id: string }) {
  const canWrite = useCan("clusters.write");
  const clusterName = useClusters().data?.find((c) => c.id === id)?.name;
  const [name, setName] = useState("");
  const [location, setLocation] = useState("");
  const [backup, setBackup] = useState("");
  const [confirm, setConfirm] = useState("");
  const base = `/clusters/${id}`;
  const query = useQuery({ queryKey: ["clusters", id, "backups"], queryFn: () => api.get<Inventory>(base + "/backups"), retry: false, refetchInterval: 10000 });
  const restore = Boolean(backup);
  const locations = query.data?.locations.filter((l) => l.writable && l.phase === "Available") ?? [];
  const storage = location || locations[0]?.name;
  const mutation = useMutation({
    mutationFn: () => restore ? api.post<Job>(base + "/restores", { name, backup, confirm })
      : api.post<Job>(base + "/backups", { name, location: storage }),
    onSuccess: () => { setName(""); setBackup(""); setConfirm(""); void query.refetch(); },
  });
  if (query.isError) return query.error instanceof ApiError && query.error.code === "not_found_in_cluster"
    ? <EmptyState title="Velero API unavailable" description="Install Velero with a backup storage location in the velero namespace to use cluster backup and restore." action={<Button variant="outline" onClick={() => query.refetch()}>Refresh</Button>} />
    : <LiveError error={query.error} clusterId={id} onRetry={() => query.refetch()} />;
  if (!query.data) return <TableSkeleton />;
  return <section className="space-y-4">
    <p className="text-sm text-muted-foreground">Velero backs up Kubernetes objects. Volume data coverage depends on the cluster’s snapshot and file-system backup configuration.</p>
    {canWrite && <form className="space-y-2 rounded-lg border p-4" onSubmit={(e) => {e.preventDefault(); mutation.mutate();}}>
      <div className="flex flex-wrap gap-2">
        <select aria-label="Backup or restore" className={selectClass} value={backup} onChange={(e) => {setBackup(e.target.value); setConfirm("");}}>
          <option value="">Create backup</option>
          {query.data.backups.filter((b) => b.phase === "Completed").map((b) => <option key={b.name} value={b.name}>Restore {b.name}</option>)}
        </select>
        {!restore && <select aria-label="Backup storage location" className={selectClass} value={storage ?? ""} onChange={(e) => setLocation(e.target.value)}>
          {!locations.length && <option value="">No writable storage location</option>}
          {locations.map((l) => <option key={l.name}>{l.name}</option>)}</select>}
        <Input aria-label="Request name" className="max-w-64" placeholder={restore ? "New restore name" : "New backup name"} value={name}
          required pattern="[a-z0-9]([-a-z0-9]*[a-z0-9])?" maxLength={63} onChange={(e) => setName(e.target.value)} />
      </div>
      {restore && <label className="block text-sm">Restore missing resources into <strong>{clusterName}</strong>. Enter the destination cluster name to confirm.
        <Input className="mt-2 max-w-64" value={confirm} onChange={(e) => setConfirm(e.target.value)} /></label>}
      <Button disabled={!name || mutation.isPending || (restore ? confirm !== clusterName : !storage)}>{restore ? "Start cluster restore" : "Start cluster backup"}</Button>
    </form>}
    {mutation.isError && <ErrorState error={mutation.error} />}
    {mutation.data && <p role="status" className="text-sm">Velero accepted {mutation.data.name}. Status updates appear below.</p>}
    {query.data.truncated.length > 0 && <p role="status">Inventory truncated for {query.data.truncated.join(", ")}.</p>}
    {(["backups", "restores"] as const).map((kind) => <section key={kind} className="space-y-2"><h2 className="font-semibold capitalize">{kind}</h2>
      {!query.data[kind].length ? <EmptyState title={`No cluster ${kind}`} /> :
        <Table><TableHeader><TableRow>{["Name","Phase",kind === "backups" ? "Location" : "Backup","Created","Warnings","Errors"].map((h) => <TableHead key={h}>{h}</TableHead>)}</TableRow></TableHeader>
          <TableBody>{query.data[kind].map((j) => <TableRow key={j.name}><TableCell>{j.name}</TableCell><TableCell>{j.phase}</TableCell>
            <TableCell>{j.location || j.backup || "—"}</TableCell><TableCell>{formatDateTime(j.created_at)}</TableCell>
            <TableCell>{j.warnings}</TableCell><TableCell>{j.errors}</TableCell></TableRow>)}</TableBody></Table>}
    </section>)}
  </section>;
}

export function BackupsPage() {
  const admin = useCan("platform.manage");
  return <div className="space-y-5"><PageHeader eyebrow="Operations" title="Backup & restore" />
    <Tabs defaultValue="cluster"><TabsList><TabsTrigger value="cluster">Cluster</TabsTrigger>
      {admin && <TabsTrigger value="platform">Platform</TabsTrigger>}</TabsList>
      <TabsContent value="cluster" className="pt-3"><ClusterScope>{(id) => <ClusterBackups key={id} id={id} />}</ClusterScope></TabsContent>
      {admin && <TabsContent value="platform" className="pt-3"><PlatformBackups /></TabsContent>}
    </Tabs></div>;
}
