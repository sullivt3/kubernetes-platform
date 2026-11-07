"use client";

import {
  AlertTriangle,
  CheckCircle2,
  ChevronRight,
  KeyRound,
  MoreHorizontal,
  Plus,
  RefreshCw,
  Server,
  ShieldAlert,
  Trash2,
  XCircle,
} from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Fragment, useState } from "react";
import { toast } from "sonner";

import { ConfirmDialog } from "@/components/common/confirm-dialog";
import { EmptyState, ErrorState, PageHeader, TableSkeleton } from "@/components/common/page";
import { StatusPill } from "@/components/common/status";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { ApiError } from "@/lib/api/client";
import { useCredentials, useDeleteMachine, useInspectMachine, useMachines, useRegisterMachine } from "@/lib/api/hooks";
import type { Machine, MachineState } from "@/lib/api/types";
import { formatRelative } from "@/lib/format";
import { cn } from "@/lib/utils";

const NAME_PATTERN = /^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$/;

export function MachineStateBadge({ state }: { state: MachineState }) {
  switch (state) {
    case "ready":
      return <StatusPill tone="success" icon={CheckCircle2}>Ready</StatusPill>;
    case "requested":
    case "provisioning":
      return <StatusPill tone="info" icon={RefreshCw} spin>Inspecting</StatusPill>;
    case "unreachable":
      return <StatusPill tone="warning" icon={AlertTriangle}>Unreachable</StatusPill>;
    case "error":
      return <StatusPill tone="danger" icon={XCircle}>Not ready</StatusPill>;
    default:
      return <StatusPill tone="neutral">{state}</StatusPill>;
  }
}

export function primaryAddress(machine: Machine): string | undefined {
  return machine.addresses.find((a) => a.type === "primary")?.address;
}

function RegisterDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const router = useRouter();
  const credentials = useCredentials();
  const register = useRegisterMachine();
  const sshCredentials = (credentials.data?.items ?? []).filter(
    (c) => c.kind === "ssh_private_key" || c.kind === "ssh_password",
  );
  const [name, setName] = useState("");
  const [host, setHost] = useState("");
  const [port, setPort] = useState("22");
  const [username, setUsername] = useState("");
  const [credentialId, setCredentialId] = useState("");
  const [error, setError] = useState<string | null>(null);

  const selected = sshCredentials.find((c) => c.id === credentialId);
  const nameError = name && !NAME_PATTERN.test(name) ? "Lowercase letters, digits and '-' (becomes the node name)" : null;
  const portNumber = Number(port);
  const portError = port && (!Number.isInteger(portNumber) || portNumber < 1 || portNumber > 65535) ? "1–65535" : null;
  const valid = name && !nameError && host.trim() && !portError && credentialId;

  const reset = () => {
    setName("");
    setHost("");
    setPort("22");
    setUsername("");
    setCredentialId("");
    setError(null);
  };

  const submit = () => {
    setError(null);
    register.mutate(
      {
        name,
        host: host.trim(),
        port: portNumber || null,
        username: username.trim() || null,
        credential_id: credentialId,
      },
      {
        onSuccess: ({ machine, operation }) => {
          toast.success(`Registered ${machine.name}`, {
            description: "Inspecting connectivity, host key, OS and hardware…",
            action: { label: "View progress", onClick: () => router.push(`/operations/${operation.id}`) },
          });
          reset();
          onOpenChange(false);
        },
        onError: (e) => setError(e instanceof ApiError ? e.message : String(e)),
      },
    );
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) reset();
        onOpenChange(next);
      }}
    >
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Register machine</DialogTitle>
          <DialogDescription>
            Add an existing Linux server. The platform connects over SSH, records the server&apos;s host key and
            checks it against Kubernetes requirements. Nothing is installed until you create a cluster.
          </DialogDescription>
        </DialogHeader>
        <form
          className="space-y-4"
          onSubmit={(e) => {
            e.preventDefault();
            if (valid) submit();
          }}
        >
          <div className="space-y-1.5">
            <Label htmlFor="m-name">Name</Label>
            <Input id="m-name" value={name} onChange={(e) => setName(e.target.value)} placeholder="node-1" aria-invalid={Boolean(nameError)} />
            <p className={cn("text-xs", nameError ? "text-red-400" : "text-muted-foreground")}>
              {nameError ?? "Used as the Kubernetes node name."}
            </p>
          </div>
          <div className="grid grid-cols-[1fr_6rem] gap-3">
            <div className="space-y-1.5">
              <Label htmlFor="m-host">Host or IP address</Label>
              <Input id="m-host" value={host} onChange={(e) => setHost(e.target.value)} placeholder="10.0.0.21" />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="m-port">SSH port</Label>
              <Input id="m-port" value={port} onChange={(e) => setPort(e.target.value)} inputMode="numeric" aria-invalid={Boolean(portError)} />
            </div>
          </div>
          <div className="space-y-1.5">
            <Label>SSH credential</Label>
            {credentials.isLoading ? (
              <Input disabled placeholder="Loading credentials…" />
            ) : sshCredentials.length === 0 ? (
              <p className="rounded-md border border-amber-500/30 bg-amber-500/5 p-2 text-sm text-amber-200">
                No SSH credentials stored yet.{" "}
                <Link href="/security/credentials" className="underline underline-offset-4">
                  Add an SSH key or password
                </Link>{" "}
                first.
              </p>
            ) : (
              <Select value={credentialId} onValueChange={setCredentialId}>
                <SelectTrigger className="w-full">
                  <SelectValue placeholder="Select a credential" />
                </SelectTrigger>
                <SelectContent>
                  {sshCredentials.map((c) => (
                    <SelectItem key={c.id} value={c.id}>
                      {c.name} {c.kind === "ssh_password" ? "(password)" : "(private key)"}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="m-user">Username (optional)</Label>
            <Input
              id="m-user"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              placeholder={selected?.public_metadata.username ?? "root"}
            />
            <p className="text-xs text-muted-foreground">
              Non-root users need sudo (passwordless sudo for key-based logins).
            </p>
          </div>
          {error && <p role="alert" className="rounded border border-red-500/30 bg-red-500/5 p-2 text-sm text-red-300">{error}</p>}
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={!valid || register.isPending}>
              {register.isPending ? "Registering…" : "Register and inspect"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function CheckIcon({ status }: { status: "pass" | "warn" | "fail" }) {
  if (status === "pass") return <CheckCircle2 className="size-4 text-emerald-400" />;
  if (status === "warn") return <AlertTriangle className="size-4 text-amber-400" />;
  return <XCircle className="size-4 text-red-400" />;
}

function MachineDetails({ machine }: { machine: Machine }) {
  const facts: [string, string | number | undefined | null][] = [
    ["Operating system", machine.os_info.pretty_name],
    ["Kernel", machine.os_info.kernel],
    ["Hostname", machine.os_info.hostname],
    ["Architecture", machine.hardware.architecture],
    ["CPU cores", machine.hardware.cpu_cores],
    ["Memory", machine.hardware.memory_mb ? `${machine.hardware.memory_mb} MB` : undefined],
    ["Free disk (/)", machine.hardware.root_disk_free_gb != null ? `${machine.hardware.root_disk_free_gb} GB` : undefined],
    ["Addresses", machine.addresses.map((a) => a.address).join(", ")],
    ["SSH", `${machine.ssh_username ?? "?"}@${machine.ssh_host}:${machine.ssh_port}`],
    ["Host key", machine.host_key_fingerprint],
    ["containerd", machine.os_info.kubernetes?.containerd],
    ["kubeadm", machine.os_info.kubernetes?.kubeadm],
  ];
  return (
    <div className="grid gap-6 bg-muted/20 px-6 py-4 lg:grid-cols-2">
      <div>
        <p className="mb-2 text-xs font-medium tracking-wide text-muted-foreground uppercase">Kubernetes prerequisites</p>
        {machine.checks.length === 0 ? (
          <p className="text-sm text-muted-foreground">Not inspected yet.</p>
        ) : (
          <ul className="space-y-1.5">
            {machine.checks.map((check) => (
              <li key={check.key} className="flex gap-2 text-sm">
                <span className="mt-0.5"><CheckIcon status={check.status} /></span>
                <span>
                  <span className="font-medium">{check.label}:</span>{" "}
                  <span className="text-muted-foreground">{check.message}</span>
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>
      <div>
        <p className="mb-2 text-xs font-medium tracking-wide text-muted-foreground uppercase">Facts</p>
        <dl className="grid grid-cols-[9rem_1fr] gap-x-3 gap-y-1 text-sm">
          {facts
            .filter(([, v]) => v !== undefined && v !== null && v !== "")
            .map(([label, value]) => (
              <Fragment key={label}>
                <dt className="text-muted-foreground">{label}</dt>
                <dd className="truncate font-mono text-xs leading-5">{value}</dd>
              </Fragment>
            ))}
        </dl>
        {machine.inspected_at && (
          <p className="mt-2 text-xs text-muted-foreground">Inspected {formatRelative(machine.inspected_at)}</p>
        )}
      </div>
    </div>
  );
}

export function MachinesPage() {
  const router = useRouter();
  const machines = useMachines();
  const inspect = useInspectMachine();
  const remove = useDeleteMachine();
  const [registering, setRegistering] = useState(false);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<Machine | null>(null);
  const [retrusting, setRetrusting] = useState<Machine | null>(null);

  const startInspect = (machine: Machine, acceptNewHostKey = false) =>
    inspect.mutate(
      { id: machine.id, acceptNewHostKey, confirm: acceptNewHostKey ? machine.name : undefined },
      {
        onSuccess: (op) => {
          setRetrusting(null);
          toast.info(`Inspecting ${machine.name}`, {
            action: { label: "View progress", onClick: () => router.push(`/operations/${op.id}`) },
          });
        },
        onError: (e) => toast.error(`Could not inspect ${machine.name}`, { description: e.message }),
      },
    );

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow="Infrastructure"
        title="Machines"
        description="Existing Linux servers the platform can install Kubernetes on. Each machine is reached over SSH with a pinned host key and checked against Kubernetes requirements."
        actions={
          <Button size="sm" onClick={() => setRegistering(true)}>
            <Plus /> Register machine
          </Button>
        }
      />
      <Card className="py-0">
        <CardContent className="px-0">
          {machines.isLoading && <div className="p-4"><TableSkeleton rows={4} columns={6} /></div>}
          {machines.isError && <div className="p-4"><ErrorState error={machines.error} onRetry={() => machines.refetch()} /></div>}
          {machines.data?.length === 0 && (
            <EmptyState
              className="m-4"
              icon={Server}
              title="No machines registered"
              description="Register the Linux servers (physical or virtual) that will become Kubernetes nodes. Supported: Ubuntu 22.04/24.04, Debian 12/13, RHEL/Rocky/AlmaLinux 8/9 with 2+ CPUs and 2 GB+ RAM."
              action={<Button variant="outline" size="sm" onClick={() => setRegistering(true)}><Plus /> Register machine</Button>}
            />
          )}
          {machines.data && machines.data.length > 0 && (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-8" />
                  <TableHead>Machine</TableHead>
                  <TableHead>State</TableHead>
                  <TableHead>Operating system</TableHead>
                  <TableHead>Resources</TableHead>
                  <TableHead>Cluster</TableHead>
                  <TableHead className="w-12" />
                </TableRow>
              </TableHeader>
              <TableBody>
                {machines.data.map((m) => {
                  const isOpen = expanded === m.id;
                  return (
                    <Fragment key={m.id}>
                      <TableRow className="cursor-pointer" onClick={() => setExpanded(isOpen ? null : m.id)}>
                        <TableCell>
                          <ChevronRight className={cn("size-4 text-muted-foreground transition-transform", isOpen && "rotate-90")} />
                        </TableCell>
                        <TableCell>
                          <p className="font-medium">{m.name}</p>
                          <p className="font-mono text-[11px] text-muted-foreground">
                            {primaryAddress(m) ?? m.ssh_host}
                            {m.ssh_host && primaryAddress(m) && primaryAddress(m) !== m.ssh_host && ` · via ${m.ssh_host}:${m.ssh_port}`}
                          </p>
                        </TableCell>
                        <TableCell>
                          <MachineStateBadge state={m.lifecycle_state} />
                          {m.last_error && (
                            <p className="mt-1 max-w-64 truncate text-xs text-red-300" title={m.last_error}>{m.last_error}</p>
                          )}
                        </TableCell>
                        <TableCell className="text-sm">{m.os_info.pretty_name ?? <span className="text-muted-foreground">—</span>}</TableCell>
                        <TableCell className="text-sm tabular-nums text-muted-foreground">
                          {m.hardware.cpu_cores ? `${m.hardware.cpu_cores} CPU · ${Math.round((m.hardware.memory_mb ?? 0) / 1024 * 10) / 10} GB` : "—"}
                        </TableCell>
                        <TableCell className="text-sm">
                          {m.cluster_name ? (
                            <span>{m.cluster_name} <span className="text-xs text-muted-foreground">({m.cluster_role === "control_plane" ? "control plane" : "worker"})</span></span>
                          ) : (
                            <span className="text-muted-foreground">Available</span>
                          )}
                        </TableCell>
                        <TableCell onClick={(e) => e.stopPropagation()}>
                          <DropdownMenu>
                            <DropdownMenuTrigger asChild>
                              <Button variant="ghost" size="icon" aria-label={`Actions for ${m.name}`}>
                                <MoreHorizontal />
                              </Button>
                            </DropdownMenuTrigger>
                            <DropdownMenuContent align="end">
                              <DropdownMenuItem onSelect={() => startInspect(m)}>
                                <RefreshCw /> Inspect again
                              </DropdownMenuItem>
                              <DropdownMenuItem onSelect={() => setRetrusting(m)}>
                                <KeyRound /> Accept new host key…
                              </DropdownMenuItem>
                              <DropdownMenuSeparator />
                              <DropdownMenuItem
                                disabled={Boolean(m.cluster_name)}
                                className="text-red-400 focus:text-red-300"
                                onSelect={() => setDeleting(m)}
                              >
                                <Trash2 /> {m.cluster_name ? "In use by a cluster" : "Remove from inventory"}
                              </DropdownMenuItem>
                            </DropdownMenuContent>
                          </DropdownMenu>
                        </TableCell>
                      </TableRow>
                      {isOpen && (
                        <TableRow className="hover:bg-transparent">
                          <TableCell colSpan={7} className="p-0 whitespace-normal">
                            <MachineDetails machine={m} />
                          </TableCell>
                        </TableRow>
                      )}
                    </Fragment>
                  );
                })}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      <RegisterDialog open={registering} onOpenChange={setRegistering} />
      <ConfirmDialog
        open={deleting !== null}
        onOpenChange={(open) => !open && setDeleting(null)}
        title={`Remove ${deleting?.name} from the inventory?`}
        description={<p>The platform forgets this machine and its pinned host key. Nothing is changed on the server itself.</p>}
        confirmText={deleting?.name}
        confirmLabel="Remove machine"
        pending={remove.isPending}
        onConfirm={() =>
          deleting &&
          remove.mutate(
            { id: deleting.id, confirm: deleting.name },
            {
              onSuccess: () => {
                toast.success(`Removed ${deleting.name}`);
                setDeleting(null);
              },
              onError: (e) => toast.error("Could not remove machine", { description: e.message }),
            },
          )
        }
      />
      <ConfirmDialog
        open={retrusting !== null}
        onOpenChange={(open) => !open && setRetrusting(null)}
        title={`Accept a new host key for ${retrusting?.name}?`}
        description={
          <>
            <p className="flex gap-2">
              <ShieldAlert className="mt-0.5 size-4 shrink-0 text-amber-400" />
              Only do this if you know the server was rebuilt or its SSH keys were regenerated. A changed host key can
              also mean the connection is being intercepted.
            </p>
            {retrusting?.host_key_fingerprint && (
              <p className="font-mono text-xs">Currently trusted: {retrusting.host_key_fingerprint}</p>
            )}
          </>
        }
        confirmText={retrusting?.name}
        confirmLabel="Trust new key and inspect"
        pending={inspect.isPending}
        onConfirm={() => retrusting && startInspect(retrusting, true)}
      />
    </div>
  );
}
