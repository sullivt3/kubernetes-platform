"use client";

import { CheckCircle2, ExternalLink, Rocket } from "lucide-react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useState } from "react";

import { EmptyState, ErrorState, PageHeader, TableSkeleton } from "@/components/common/page";
import { OperationStatusBadge } from "@/components/common/status";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { ApiError } from "@/lib/api/client";
import { useCredentials, useMachines } from "@/lib/api/hooks";
import { expandNames, useConnections, useOperationResult, useProvisionMachines } from "@/lib/api/infra";
import type { Machine, OperationDetail } from "@/lib/api/types";
import { ConnectionStatus } from "./connections";

const NAME_PATTERN = /^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$/;
const USER_PATTERN = /^[a-z_][a-z0-9_-]{0,31}$/;

const PRESETS = [
  { label: "Small · 2 CPU, 2 GB", cpu: 2, memory: 2048, disk: 20 },
  { label: "Medium · 2 CPU, 4 GB", cpu: 2, memory: 4096, disk: 40 },
  { label: "Large · 4 CPU, 8 GB", cpu: 4, memory: 8192, disk: 80 },
];

function ProvisionProgress({ machine, operation }: { machine: Machine; operation: OperationDetail }) {
  const op = useOperationResult(operation.id);
  const current = op.data ?? operation;
  const step = current.steps?.find((s) => s.status === "running");
  return (
    <li className="flex items-center justify-between gap-3 rounded-md border border-border/70 px-3 py-2 text-sm">
      <span className="font-medium">{machine.name}</span>
      <span className="flex items-center gap-3 text-muted-foreground">
        {step && <span className="text-xs">{step.name}…</span>}
        {current.status === "failed" && <span className="max-w-80 truncate text-xs text-red-300" title={current.error?.message}>{current.error?.message}</span>}
        <OperationStatusBadge status={current.status} />
        <Link href={`/operations/${operation.id}`} className="text-xs underline underline-offset-4">Logs</Link>
      </span>
    </li>
  );
}

function ProvisionForm() {
  const params = useSearchParams();
  const connections = useConnections();
  const credentials = useCredentials();
  const machines = useMachines();
  const provision = useProvisionMachines();
  const usable = (connections.data ?? []).filter((c) => c.capabilities.includes("provision"));
  const keys = (credentials.data?.items ?? []).filter((c) => c.kind === "ssh_private_key");
  const [connectionId, setConnectionId] = useState(params.get("connection") ?? "");
  const [prefix, setPrefix] = useState("node");
  const [count, setCount] = useState("2");
  const [cpu, setCpu] = useState("2");
  const [memory, setMemory] = useState("2048");
  const [disk, setDisk] = useState("20");
  const [credentialId, setCredentialId] = useState("");
  const [username, setUsername] = useState("platform");
  const [bridge, setBridge] = useState("");
  const [ipconfig, setIpconfig] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [started, setStarted] = useState<{ machine: Machine; operation: OperationDetail }[]>([]);

  const connection = usable.find((c) => c.id === connectionId) ?? (usable.length === 1 ? usable[0] : undefined);
  const existing = new Set((machines.data ?? []).map((m) => m.name));
  const n = Number(count);
  const names = Number.isInteger(n) && n >= 1 && n <= 20 ? expandNames(prefix, n, nextIndex(prefix, existing)) : [];
  const problems = [
    !connection && "Choose a connection",
    connection?.status === "error" && "The connection failed validation; fix it on the Providers page",
    !NAME_PATTERN.test(`${prefix}-1`) && "Name prefix: lowercase letters, digits and '-'",
    !names.length && "Count: 1–20 machines",
    !(Number(cpu) >= 1) && "CPU: at least 1",
    !(Number(memory) >= 512) && "Memory: at least 512 MB",
    !(Number(disk) >= 10) && "Disk: at least 10 GB",
    !credentialId && "Choose the SSH key to install",
    !USER_PATTERN.test(username) && "Username: lowercase Linux user name",
  ].filter(Boolean) as string[];

  const submit = () => {
    if (!connection) return;
    setError(null);
    const providerOptions: Record<string, unknown> = {};
    if (ipconfig.trim()) providerOptions.ipconfig = ipconfig.trim();
    provision.mutate(
      {
        connectionId: connection.id,
        names,
        cpu_cores: Number(cpu),
        memory_mb: Number(memory),
        disk_gb: Number(disk),
        credential_id: credentialId,
        ssh_username: username,
        network: bridge.trim() || null,
        provider_options: providerOptions,
      },
      {
        onSuccess: (result) => setStarted(result),
        onError: (e) => setError(e instanceof ApiError ? e.message : String(e)),
      },
    );
  };

  if (connections.isLoading) return <TableSkeleton rows={4} columns={2} />;
  if (connections.isError) return <ErrorState error={connections.error} onRetry={() => connections.refetch()} />;
  if (usable.length === 0)
    return (
      <EmptyState
        icon={Rocket}
        title="No provider that can provision machines"
        description="Add a Local (Docker) or Proxmox VE connection first. Existing servers can be registered directly under Machines."
        action={<Button asChild size="sm" variant="outline"><Link href="/infrastructure/providers">Add a connection</Link></Button>}
      />
    );

  return (
    <div className="grid gap-6 xl:grid-cols-[1fr_24rem]">
      <Card>
        <CardContent className="space-y-6">
          <div className="grid gap-4 md:grid-cols-2">
            <div className="space-y-1.5">
              <Label>Connection</Label>
              <Select value={connection?.id ?? ""} onValueChange={setConnectionId}>
                <SelectTrigger className="w-full"><SelectValue placeholder="Choose a connection" /></SelectTrigger>
                <SelectContent>
                  {usable.map((c) => <SelectItem key={c.id} value={c.id}>{c.name} · {c.provider_name}</SelectItem>)}
                </SelectContent>
              </Select>
              {connection && <div className="pt-1"><ConnectionStatus connection={connection} /></div>}
            </div>
            <div className="grid grid-cols-[1fr_6rem] gap-3">
              <div className="space-y-1.5">
                <Label htmlFor="p-prefix">Name prefix</Label>
                <Input id="p-prefix" value={prefix} onChange={(e) => setPrefix(e.target.value)} />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="p-count">Count</Label>
                <Input id="p-count" value={count} onChange={(e) => setCount(e.target.value)} inputMode="numeric" />
              </div>
            </div>
          </div>
          <div className="space-y-2">
            <Label>Size</Label>
            <div className="flex flex-wrap gap-2">
              {PRESETS.map((p) => (
                <Button
                  key={p.label}
                  type="button"
                  size="sm"
                  variant={Number(cpu) === p.cpu && Number(memory) === p.memory && Number(disk) === p.disk ? "secondary" : "outline"}
                  onClick={() => {
                    setCpu(String(p.cpu));
                    setMemory(String(p.memory));
                    setDisk(String(p.disk));
                  }}
                >
                  {p.label}
                </Button>
              ))}
            </div>
            <div className="grid grid-cols-3 gap-3">
              <div className="space-y-1.5"><Label htmlFor="p-cpu" className="text-xs text-muted-foreground">CPU cores</Label><Input id="p-cpu" value={cpu} onChange={(e) => setCpu(e.target.value)} inputMode="numeric" /></div>
              <div className="space-y-1.5"><Label htmlFor="p-mem" className="text-xs text-muted-foreground">Memory (MB)</Label><Input id="p-mem" value={memory} onChange={(e) => setMemory(e.target.value)} inputMode="numeric" /></div>
              <div className="space-y-1.5"><Label htmlFor="p-disk" className="text-xs text-muted-foreground">Disk (GB)</Label><Input id="p-disk" value={disk} onChange={(e) => setDisk(e.target.value)} inputMode="numeric" /></div>
            </div>
            {connection?.provider_type === "local" && (
              <p className="text-xs text-muted-foreground">
                Local machines share this computer&apos;s CPU, memory and disk. CPU and memory become container limits
                (when enabled on the connection); the disk size is not enforced.
              </p>
            )}
          </div>
          <div className="grid gap-4 md:grid-cols-2">
            <div className="space-y-1.5">
              <Label>SSH key to install</Label>
              {keys.length === 0 ? (
                <p className="rounded-md border border-amber-500/30 bg-amber-500/5 p-2 text-sm text-amber-200">
                  No SSH private key stored. <Link href="/security/credentials" className="underline underline-offset-4">Add one</Link>; its public key is installed on the new machines.
                </p>
              ) : (
                <Select value={credentialId} onValueChange={setCredentialId}>
                  <SelectTrigger className="w-full"><SelectValue placeholder="Select a key" /></SelectTrigger>
                  <SelectContent>{keys.map((c) => <SelectItem key={c.id} value={c.id}>{c.name}</SelectItem>)}</SelectContent>
                </Select>
              )}
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="p-user">Login user</Label>
              <Input id="p-user" value={username} onChange={(e) => setUsername(e.target.value)} />
              <p className="text-xs text-muted-foreground">Created with passwordless sudo.</p>
            </div>
          </div>
          {connection?.provider_type === "proxmox" && (
            <div className="grid gap-4 md:grid-cols-2">
              <div className="space-y-1.5">
                <Label htmlFor="p-bridge">Network bridge (optional)</Label>
                <Input id="p-bridge" value={bridge} onChange={(e) => setBridge(e.target.value)} placeholder="template default" className="font-mono text-xs" />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="p-ip">IP configuration (optional)</Label>
                <Input id="p-ip" value={ipconfig} onChange={(e) => setIpconfig(e.target.value)} placeholder="ip=dhcp" className="font-mono text-xs" />
                <p className="text-xs text-muted-foreground">cloud-init ipconfig0, e.g. ip=10.0.0.21/24,gw=10.0.0.1 (one machine).</p>
              </div>
            </div>
          )}
          {problems.length > 0 && (
            <ul className="space-y-1 text-sm text-amber-300/90">{problems.map((p) => <li key={p}>• {p}</li>)}</ul>
          )}
          {error && <p role="alert" className="rounded border border-red-500/30 bg-red-500/5 p-2 text-sm text-red-300">{error}</p>}
          <div className="flex justify-end border-t border-border/60 pt-4">
            <Button disabled={problems.length > 0 || provision.isPending} onClick={submit}>
              <Rocket /> {provision.isPending ? "Starting…" : `Provision ${names.length || ""} machine${names.length === 1 ? "" : "s"}`}
            </Button>
          </div>
        </CardContent>
      </Card>
      <div className="space-y-3">
        <p className="text-sm font-medium">Plan</p>
        <Card>
          <CardContent className="space-y-3 text-sm">
            <p className="text-muted-foreground">Each machine is created, started, reached over SSH (host key pinned on first contact) and inspected against Kubernetes requirements.</p>
            <ul className="space-y-1 font-mono text-xs">{names.map((name) => <li key={name}>{name}</li>)}</ul>
          </CardContent>
        </Card>
        {started.length > 0 && (
          <div className="space-y-2">
            <p className="flex items-center gap-2 text-sm font-medium"><CheckCircle2 className="size-4 text-emerald-400" /> Provisioning started</p>
            <ul className="space-y-2">
              {started.map((s) => <ProvisionProgress key={s.operation.id} machine={s.machine} operation={s.operation} />)}
            </ul>
            <Button asChild size="sm" variant="outline"><Link href="/infrastructure/machines"><ExternalLink /> Machines</Link></Button>
          </div>
        )}
      </div>
    </div>
  );
}

function nextIndex(prefix: string, existing: Set<string>): number {
  let i = 1;
  while (existing.has(`${prefix}-${i}`)) i += 1;
  return i;
}

export function ProvisioningPage() {
  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow="Infrastructure"
        title="Provision machines"
        description="Create new Linux machines through a provider connection. They join the machine inventory and can be used for clusters as soon as they are ready."
      />
      <ProvisionForm />
    </div>
  );
}
