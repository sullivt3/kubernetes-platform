"use client";

import {
  AlertTriangle,
  CheckCircle2,
  CircleHelp,
  Download,
  MoreHorizontal,
  Plug,
  Plus,
  Radar,
  RefreshCw,
  Rocket,
  Trash2,
  XCircle,
} from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useMemo, useState } from "react";
import { toast } from "sonner";

import { ConfirmDialog } from "@/components/common/confirm-dialog";
import { EmptyState, ErrorState, TableSkeleton } from "@/components/common/page";
import {
  missingRequired,
  SchemaForm,
  schemaDefaults,
  schemaPayload,
  type SchemaValues,
} from "@/components/common/schema-form";
import { ImplementationBadge, StatusPill } from "@/components/common/status";
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
import { useCredentials, useOperations, useProviders } from "@/lib/api/hooks";
import {
  useConnections,
  useCreateConnection,
  useDeleteConnection,
  useDiscoverMachines,
  useImportMachine,
  useOperationResult,
  useValidateConnection,
} from "@/lib/api/infra";
import type { DiscoveredMachine, ProviderConnection, ProviderDescriptor } from "@/lib/api/types";
import { formatRelative, humanize } from "@/lib/format";

const NAME_PATTERN = /^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$/;

export function ConnectionStatus({ connection }: { connection: ProviderConnection }) {
  if (connection.status === "connected") return <StatusPill tone="success" icon={CheckCircle2}>Connected</StatusPill>;
  if (connection.status === "error") return <StatusPill tone="danger" icon={XCircle}>Failed</StatusPill>;
  return <StatusPill tone="info" icon={RefreshCw} spin>Validating</StatusPill>;
}

/** Provider-specific hints that the schema alone cannot express. */
const SETUP_HINTS: Record<string, string> = {
  local:
    "Machines are privileged containers on the Docker host this platform's workers use. The first machine builds the node image (needs internet access). Docker access equals root on that host.",
  proxmox:
    "Create an API token (Datacenter → Permissions → API Tokens) and store its secret as a Proxmox API token credential. Clone source: a VM template with a cloud-init drive and the QEMU guest agent enabled. Self-signed certificates: paste the CA or the SHA-256 fingerprint shown under the node's Certificates.",
  aws: "Store the secret access key as an AWS access key credential; the access key ID goes in this form. Needs sts:GetCallerIdentity, ec2:DescribeInstances and ec2:DescribeInstanceTypes.",
  azure:
    "Store the service principal's client secret as an Azure service principal credential. Grant it the Reader role on the subscription.",
  gcp: "Store a service account key (JSON) as a GCP service account credential. Grant it roles/compute.viewer on the project.",
};

function AddConnectionDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const router = useRouter();
  const providers = useProviders();
  const credentials = useCredentials();
  const create = useCreateConnection();
  const choices = (providers.data ?? []).filter((p) => p.implementation_status !== "planned" && p.type !== "ssh");
  const [type, setType] = useState("");
  const [name, setName] = useState("");
  const [credentialId, setCredentialId] = useState("");
  const [values, setValues] = useState<SchemaValues>({});
  const [error, setError] = useState<string | null>(null);
  const provider = choices.find((p) => p.type === type);
  const usable = (credentials.data?.items ?? []).filter((c) => provider?.credential_kinds.includes(c.kind));
  const missing = provider ? missingRequired(provider.config_schema, values) : [];
  const valid = provider && name.trim().length >= 2 && missing.length === 0 && (!provider.credential_required || credentialId);

  const choose = (next: string) => {
    const p = choices.find((c) => c.type === next);
    setType(next);
    setCredentialId("");
    setValues(p ? schemaDefaults(p.config_schema) : {});
    if (!name && p) setName(p.type === "local" ? "Workstation" : p.display_name);
  };
  const reset = () => {
    setType("");
    setName("");
    setCredentialId("");
    setValues({});
    setError(null);
  };
  const submit = () => {
    if (!provider) return;
    setError(null);
    create.mutate(
      {
        name: name.trim(),
        provider_type: provider.type,
        config: schemaPayload(provider.config_schema, values),
        credential_id: credentialId || null,
      },
      {
        onSuccess: ({ connection, operation }) => {
          toast.success(`Added ${connection.name}`, {
            description: "Validating the connection…",
            action: { label: "View progress", onClick: () => router.push(`/operations/${operation.id}`) },
          });
          reset();
          onOpenChange(false);
        },
        onError: (e) => {
          const details = e instanceof ApiError ? (e.details?.errors as { loc: string[]; msg: string }[] | undefined) : undefined;
          setError(
            details?.length
              ? details.map((d) => `${d.loc.join(".")}: ${d.msg}`).join("; ")
              : e instanceof ApiError
                ? e.message
                : String(e),
          );
        },
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
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>Add provider connection</DialogTitle>
          <DialogDescription>
            Connect a source of machines. The connection is validated immediately; nothing is created until you
            provision machines.
          </DialogDescription>
        </DialogHeader>
        <form
          className="space-y-5"
          onSubmit={(e) => {
            e.preventDefault();
            if (valid) submit();
          }}
        >
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label>Provider</Label>
              <Select value={type} onValueChange={choose}>
                <SelectTrigger className="w-full"><SelectValue placeholder="Choose a provider" /></SelectTrigger>
                <SelectContent>
                  {choices.map((p) => (
                    <SelectItem key={p.type} value={p.type}>
                      {p.display_name}
                      {p.implementation_status === "preview" ? " (preview)" : ""}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="conn-name">Name</Label>
              <Input id="conn-name" value={name} onChange={(e) => setName(e.target.value)} placeholder="Proxmox lab" />
            </div>
          </div>
          {provider && (
            <>
              <div className="space-y-2 rounded-lg border border-border/70 bg-muted/20 p-3 text-sm">
                <div className="flex items-center gap-2">
                  <ImplementationBadge status={provider.implementation_status} phase={provider.planned_phase} />
                  <span className="text-muted-foreground">{provider.description}</span>
                </div>
                {SETUP_HINTS[provider.type] && <p className="text-xs text-muted-foreground">{SETUP_HINTS[provider.type]}</p>}
                {provider.implementation_status === "preview" && (
                  <p className="flex gap-1.5 text-xs text-amber-300/90">
                    <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
                    Preview: implemented against the provider&apos;s documented API and tested with simulated
                    responses, but not yet verified against a live account. Report anything unexpected.
                  </p>
                )}
              </div>
              {provider.credential_kinds.length > 0 && (
                <div className="space-y-1.5">
                  <Label>Credential{provider.credential_required ? "" : " (optional)"}</Label>
                  {usable.length === 0 ? (
                    <p className="rounded-md border border-amber-500/30 bg-amber-500/5 p-2 text-sm text-amber-200">
                      No {provider.credential_kinds.map(humanize).join(" / ")} credential stored yet.{" "}
                      <Link href="/security/credentials" className="underline underline-offset-4">Add one</Link> first.
                    </p>
                  ) : (
                    <Select value={credentialId} onValueChange={setCredentialId}>
                      <SelectTrigger className="w-full"><SelectValue placeholder="Select a credential" /></SelectTrigger>
                      <SelectContent>
                        {usable.map((c) => <SelectItem key={c.id} value={c.id}>{c.name}</SelectItem>)}
                      </SelectContent>
                    </Select>
                  )}
                </div>
              )}
              <SchemaForm schema={provider.config_schema} values={values} onChange={setValues} />
            </>
          )}
          {error && <p role="alert" className="rounded border border-red-500/30 bg-red-500/5 p-2 text-sm text-red-300">{error}</p>}
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
            <Button type="submit" disabled={!valid || create.isPending}>
              {create.isPending ? "Adding…" : "Add and validate"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export function ConnectionsSection({ categories, providers }: { categories?: string[]; providers?: ProviderDescriptor[] }) {
  const router = useRouter();
  const connections = useConnections();
  const validate = useValidateConnection();
  const remove = useDeleteConnection();
  const [adding, setAdding] = useState(false);
  const [deleting, setDeleting] = useState<ProviderConnection | null>(null);
  const [discovering, setDiscovering] = useState<ProviderConnection | null>(null);
  const rows = (connections.data ?? []).filter(
    (c) => c.provider_type !== "ssh" && (!categories || categories.includes(c.category)),
  );

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-3">
        <div>
          <h2 className="font-semibold">Connections</h2>
          <p className="text-sm text-muted-foreground">Configured provider accounts and hosts the platform can use.</p>
        </div>
        <Button size="sm" onClick={() => setAdding(true)}><Plus /> Add connection</Button>
      </div>
      <Card className="py-0">
        <CardContent className="px-0">
          {connections.isLoading && <div className="p-4"><TableSkeleton rows={2} columns={5} /></div>}
          {connections.isError && <div className="p-4"><ErrorState error={connections.error} onRetry={() => connections.refetch()} /></div>}
          {connections.data && rows.length === 0 && (
            <EmptyState
              className="m-4"
              icon={Plug}
              title="No provider connections"
              description="Add a connection to provision machines locally (Docker) or on Proxmox, or to inventory cloud accounts. Existing Linux servers do not need one: register them under Machines."
              action={<Button variant="outline" size="sm" onClick={() => setAdding(true)}><Plus /> Add connection</Button>}
            />
          )}
          {rows.length > 0 && (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Connection</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Machines</TableHead>
                  <TableHead>Validated</TableHead>
                  <TableHead className="w-12" />
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map((c) => {
                  const canProvision = c.capabilities.includes("provision");
                  const descriptor = providers?.find((p) => p.type === c.provider_type);
                  return (
                    <TableRow key={c.id}>
                      <TableCell>
                        <p className="font-medium">{c.name}</p>
                        <p className="text-xs text-muted-foreground">
                          {c.provider_name}
                          {c.implementation_status === "preview" && " · preview"}
                        </p>
                      </TableCell>
                      <TableCell className="max-w-96 whitespace-normal">
                        <ConnectionStatus connection={c} />
                        {c.status_message && (
                          <p className={c.status === "error" ? "mt-1 text-xs text-red-300" : "mt-1 text-xs text-muted-foreground"}>
                            {c.status_message}
                          </p>
                        )}
                      </TableCell>
                      <TableCell className="tabular-nums">{c.machine_count}</TableCell>
                      <TableCell className="text-sm text-muted-foreground">
                        {c.last_validated_at ? formatRelative(c.last_validated_at) : "—"}
                      </TableCell>
                      <TableCell>
                        <DropdownMenu>
                          <DropdownMenuTrigger asChild>
                            <Button variant="ghost" size="icon" aria-label={`Actions for ${c.name}`}><MoreHorizontal /></Button>
                          </DropdownMenuTrigger>
                          <DropdownMenuContent align="end">
                            <DropdownMenuItem
                              onSelect={() =>
                                validate.mutate(c.id, {
                                  onSuccess: (op) =>
                                    toast.info(`Validating ${c.name}`, {
                                      action: { label: "View", onClick: () => router.push(`/operations/${op.id}`) },
                                    }),
                                  onError: (e) => toast.error("Could not validate", { description: e.message }),
                                })
                              }
                            >
                              <RefreshCw /> Validate again
                            </DropdownMenuItem>
                            <DropdownMenuItem onSelect={() => setDiscovering(c)} disabled={!c.capabilities.includes("discover")}>
                              <Radar /> Discover machines
                            </DropdownMenuItem>
                            {canProvision ? (
                              <DropdownMenuItem asChild>
                                <Link href={`/infrastructure/provisioning?connection=${c.id}`}><Rocket /> Provision machines</Link>
                              </DropdownMenuItem>
                            ) : (
                              <DropdownMenuItem disabled>
                                <Rocket /> Provisioning
                                {descriptor?.planned_capabilities_phase ? ` · Phase ${descriptor.planned_capabilities_phase}` : " unavailable"}
                              </DropdownMenuItem>
                            )}
                            <DropdownMenuSeparator />
                            <DropdownMenuItem
                              className="text-red-400 focus:text-red-300"
                              disabled={c.machine_count > 0}
                              onSelect={() => setDeleting(c)}
                            >
                              <Trash2 /> {c.machine_count > 0 ? "Has machines" : "Delete connection"}
                            </DropdownMenuItem>
                          </DropdownMenuContent>
                        </DropdownMenu>
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      <AddConnectionDialog open={adding} onOpenChange={setAdding} />
      <DiscoveryDialog connection={discovering} onClose={() => setDiscovering(null)} />
      <ConfirmDialog
        open={deleting !== null}
        onOpenChange={(open) => !open && setDeleting(null)}
        title={`Delete connection ${deleting?.name}?`}
        description={<p>The platform forgets this connection. Nothing changes at the provider.</p>}
        confirmText={deleting?.name}
        confirmLabel="Delete connection"
        pending={remove.isPending}
        onConfirm={() =>
          deleting &&
          remove.mutate(
            { id: deleting.id, confirm: deleting.name },
            {
              onSuccess: () => {
                toast.success(`Deleted ${deleting.name}`);
                setDeleting(null);
              },
              onError: (e) => toast.error("Could not delete", { description: e.message }),
            },
          )
        }
      />
    </div>
  );
}

// ---- Discovery -------------------------------------------------------------------------

function DiscoveryDialog({ connection, onClose }: { connection: ProviderConnection | null; onClose: () => void }) {
  return (
    <Dialog open={connection !== null} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-4xl">
        <DialogHeader>
          <DialogTitle>Machines on {connection?.name}</DialogTitle>
          <DialogDescription>A read-only inventory straight from the provider.</DialogDescription>
        </DialogHeader>
        {connection && <ConnectionInventory connection={connection} autoStart />}
      </DialogContent>
    </Dialog>
  );
}

/** Latest discovery result for a connection, with a button to refresh it. */
export function ConnectionInventory({ connection, autoStart = false }: { connection: ProviderConnection; autoStart?: boolean }) {
  const recent = useOperations({ type: "provider.discover", limit: 100 });
  const discover = useDiscoverMachines();
  const [started, setStarted] = useState<string | null>(null);
  const latest = useMemo(
    () => recent.data?.items.find((op) => op.target_id === connection.id)?.id ?? null,
    [recent.data, connection.id],
  );
  const opId = started ?? latest;
  const op = useOperationResult(opId);
  const [autoStarted, setAutoStarted] = useState(false);
  const run = () =>
    discover.mutate(connection.id, {
      onSuccess: (o) => setStarted(o.id),
      onError: (e) => toast.error("Discovery failed to start", { description: e.message }),
    });
  if (autoStart && !autoStarted && recent.isSuccess && !discover.isPending) {
    setAutoStarted(true);
    if (!latest) run();
  }

  const machines = (op.data?.result?.machines as DiscoveredMachine[] | undefined) ?? [];
  const running = op.data && !["succeeded", "failed", "cancelled"].includes(op.data.status);
  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-3 text-sm text-muted-foreground">
        <span>
          {op.data?.status === "succeeded" && op.data.finished_at && `Discovered ${formatRelative(op.data.finished_at)}`}
          {running && "Discovering…"}
          {!opId && !discover.isPending && "Not discovered yet."}
        </span>
        <Button size="sm" variant="outline" onClick={run} disabled={discover.isPending || Boolean(running)}>
          <Radar /> {opId ? "Discover again" : "Discover"}
        </Button>
      </div>
      {(running || discover.isPending) && <TableSkeleton rows={3} columns={5} />}
      {op.data?.status === "failed" && (
        <p className="rounded border border-red-500/30 bg-red-500/5 p-2 text-sm text-red-300">
          {String(op.data.error?.message ?? "Discovery failed")}
        </p>
      )}
      {op.data?.status === "succeeded" && machines.length === 0 && (
        <EmptyState icon={CircleHelp} title="No machines found" description="The provider reported no machines." />
      )}
      {op.data?.status === "succeeded" && machines.length > 0 && <DiscoveredTable connection={connection} machines={machines} />}
    </div>
  );
}

function DiscoveredTable({ connection, machines }: { connection: ProviderConnection; machines: DiscoveredMachine[] }) {
  const [importing, setImporting] = useState<DiscoveredMachine | null>(null);
  const readOnly = connection.category === "cloud";
  return (
    <>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Name</TableHead>
            <TableHead>Power</TableHead>
            <TableHead>Addresses</TableHead>
            <TableHead>Size</TableHead>
            <TableHead>Details</TableHead>
            <TableHead className="text-right">Inventory</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {machines.map((m) => (
            <TableRow key={m.external_id}>
              <TableCell>
                <p className="font-medium">{m.name}</p>
                <p className="font-mono text-[11px] text-muted-foreground">{m.external_id}</p>
              </TableCell>
              <TableCell>
                <StatusPill tone={m.power_state === "running" ? "success" : "neutral"}>{humanize(m.power_state)}</StatusPill>
              </TableCell>
              <TableCell className="font-mono text-xs">{m.addresses.map((a) => a.address).join(", ") || "—"}</TableCell>
              <TableCell className="text-sm tabular-nums text-muted-foreground">
                {m.hardware?.cpu_cores ? `${m.hardware.cpu_cores} CPU` : ""}
                {m.hardware?.memory_mb ? ` · ${Math.round(m.hardware.memory_mb / 102.4) / 10} GB` : ""}
                {!m.hardware?.cpu_cores && !m.hardware?.memory_mb && "—"}
              </TableCell>
              <TableCell className="max-w-56 truncate text-xs text-muted-foreground" title={Object.entries(m.provider_metadata).map(([k, v]) => `${k}: ${v}`).join("\n")}>
                {Object.entries(m.provider_metadata)
                  .filter(([, v]) => v)
                  .slice(0, 3)
                  .map(([k, v]) => `${humanize(k)}: ${v}`)
                  .join(" · ")}
              </TableCell>
              <TableCell className="text-right">
                {m.inventory_machine ? (
                  <StatusPill tone="success">{m.inventory_machine}</StatusPill>
                ) : readOnly || !m.importable ? (
                  <span className="text-xs text-muted-foreground">Read-only</span>
                ) : (
                  <Button size="xs" variant="outline" onClick={() => setImporting(m)}><Download /> Import</Button>
                )}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
      {readOnly && (
        <p className="text-xs text-muted-foreground">
          Cloud instances are inventoried read-only. Creating and managing cloud machines is planned for Phase 5; to
          use an existing instance today, register it as an SSH machine.
        </p>
      )}
      <ImportDialog connection={connection} machine={importing} onClose={() => setImporting(null)} />
    </>
  );
}

function ImportDialog({
  connection,
  machine,
  onClose,
}: {
  connection: ProviderConnection;
  machine: DiscoveredMachine | null;
  onClose: () => void;
}) {
  const router = useRouter();
  const credentials = useCredentials();
  const importMachine = useImportMachine();
  const ssh = (credentials.data?.items ?? []).filter((c) => c.kind === "ssh_private_key" || c.kind === "ssh_password");
  const [form, setForm] = useState({ name: "", host: "", port: "22", username: "", credentialId: "" });
  const [key, setKey] = useState<string | null>(null);
  if (machine && key !== machine.external_id) {
    setKey(machine.external_id);
    setForm({
      name: machine.name.toLowerCase().replace(/[^a-z0-9-]/g, "-").replace(/^-+|-+$/g, "").slice(0, 63),
      host: machine.ssh_endpoint?.host ?? machine.addresses[0]?.address ?? "",
      port: String(machine.ssh_endpoint?.port ?? 22),
      username: connection.provider_type === "local" ? "platform" : "",
      credentialId: "",
    });
  }
  const nameOk = NAME_PATTERN.test(form.name);
  const valid = machine && nameOk && form.host && form.credentialId && Number(form.port) > 0;
  const submit = () =>
    machine &&
    importMachine.mutate(
      {
        connectionId: connection.id,
        external_id: machine.external_id,
        name: form.name,
        host: form.host,
        port: Number(form.port),
        username: form.username || null,
        credential_id: form.credentialId,
        kube_api_host: machine.kube_api_endpoint?.host ?? null,
        kube_api_port: machine.kube_api_endpoint?.port ?? null,
      },
      {
        onSuccess: ({ operation }) => {
          toast.success(`Imported ${form.name}`, {
            description: "Inspecting over SSH…",
            action: { label: "View progress", onClick: () => router.push(`/operations/${operation.id}`) },
          });
          onClose();
        },
        onError: (e) => toast.error("Import failed", { description: e.message }),
      },
    );
  return (
    <Dialog open={machine !== null} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Import {machine?.name}</DialogTitle>
          <DialogDescription>
            Adds this machine to the inventory and inspects it over SSH. The platform pins its host key on first contact.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="imp-name">Name</Label>
            <Input id="imp-name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} aria-invalid={!nameOk} />
            {!nameOk && <p className="text-xs text-red-400">Lowercase letters, digits and &apos;-&apos; (becomes the node name)</p>}
          </div>
          <div className="grid grid-cols-[1fr_6rem] gap-3">
            <div className="space-y-1.5">
              <Label htmlFor="imp-host">SSH host</Label>
              <Input id="imp-host" value={form.host} onChange={(e) => setForm({ ...form, host: e.target.value })} />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="imp-port">Port</Label>
              <Input id="imp-port" value={form.port} onChange={(e) => setForm({ ...form, port: e.target.value })} inputMode="numeric" />
            </div>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1.5">
              <Label>SSH credential</Label>
              <Select value={form.credentialId} onValueChange={(v) => setForm({ ...form, credentialId: v })}>
                <SelectTrigger className="w-full"><SelectValue placeholder="Select" /></SelectTrigger>
                <SelectContent>{ssh.map((c) => <SelectItem key={c.id} value={c.id}>{c.name}</SelectItem>)}</SelectContent>
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="imp-user">Username</Label>
              <Input id="imp-user" value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} placeholder="from credential" />
            </div>
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>Cancel</Button>
          <Button disabled={!valid || importMachine.isPending} onClick={submit}>
            {importMachine.isPending ? "Importing…" : "Import and inspect"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** Inventory pages: one card per connection of the given categories. */
export function ProviderInventory({ categories, emptyHint }: { categories: string[]; emptyHint: string }) {
  const connections = useConnections();
  const rows = (connections.data ?? []).filter((c) => categories.includes(c.category));
  if (connections.isLoading) return <TableSkeleton rows={3} columns={4} />;
  if (connections.isError) return <ErrorState error={connections.error} onRetry={() => connections.refetch()} />;
  if (rows.length === 0)
    return (
      <EmptyState
        icon={Plug}
        title="No connections"
        description={emptyHint}
        action={<Button asChild variant="outline" size="sm"><Link href="/infrastructure/providers"><Plus /> Add a connection</Link></Button>}
      />
    );
  return (
    <div className="space-y-6">
      {rows.map((c) => (
        <Card key={c.id}>
          <CardContent className="space-y-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div>
                <p className="font-semibold">{c.name}</p>
                <p className="text-xs text-muted-foreground">{c.provider_name}</p>
              </div>
              <ConnectionStatus connection={c} />
            </div>
            {c.status === "error" ? (
              <p className="text-sm text-red-300">{c.status_message}</p>
            ) : (
              <ConnectionInventory connection={c} autoStart />
            )}
          </CardContent>
        </Card>
      ))}
    </div>
  );
}
