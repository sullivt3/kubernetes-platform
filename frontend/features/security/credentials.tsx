"use client";

import { KeyRound, Lock, Plus, Trash2 } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import { ConfirmDialog } from "@/components/common/confirm-dialog";
import { EmptyState, ErrorState, PageHeader, TableSkeleton } from "@/components/common/page";
import { Badge } from "@/components/ui/badge";
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
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Textarea } from "@/components/ui/textarea";
import { ApiError } from "@/lib/api/client";
import { useCreateCredential, useCredentials, useDeleteCredential } from "@/lib/api/hooks";
import { CREDENTIAL_KINDS, type Credential, type CredentialKind } from "@/lib/api/types";
import { formatRelative, humanize } from "@/lib/format";

const NAME_PATTERN = /^[a-z0-9]([-a-z0-9_.]{0,126}[a-z0-9])?$/;

const SECRET_HINT: Record<CredentialKind, string> = {
  ssh_private_key: "PEM or OpenSSH private key (-----BEGIN … PRIVATE KEY-----)",
  ssh_password: "SSH password",
  kubeconfig: "Complete kubeconfig YAML",
  proxmox_api_token: "Proxmox API token secret",
  vsphere_password: "vCenter password",
  aws_access_key: "AWS secret access key (put the access key ID in metadata)",
  azure_service_principal: "Service principal client secret",
  gcp_service_account: "Service account key JSON",
  generic_token: "Token value",
};

interface MetadataRow {
  key: string;
  value: string;
}

function CreateCredentialDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const create = useCreateCredential();
  const [name, setName] = useState("");
  const [kind, setKind] = useState<CredentialKind>("ssh_private_key");
  const [secret, setSecret] = useState("");
  const [description, setDescription] = useState("");
  const [metadata, setMetadata] = useState<MetadataRow[]>([]);
  const [serverError, setServerError] = useState<string | null>(null);

  const nameError = name && !NAME_PATTERN.test(name) ? "Use lowercase letters, digits, '-', '_' or '.'" : null;
  const valid = name && !nameError && secret.trim().length > 0;

  const reset = () => {
    setName("");
    setKind("ssh_private_key");
    setSecret("");
    setDescription("");
    setMetadata([]);
    setServerError(null);
  };

  const submit = () => {
    setServerError(null);
    const public_metadata = Object.fromEntries(
      metadata.filter((m) => m.key.trim()).map((m) => [m.key.trim(), m.value]),
    );
    create.mutate(
      { name, kind, secret_value: secret, description: description || null, public_metadata },
      {
        onSuccess: (credential) => {
          toast.success(`Credential "${credential.name}" stored`, { description: "Encrypted at rest; the value cannot be viewed again." });
          reset();
          onOpenChange(false);
        },
        onError: (error) => setServerError(error instanceof ApiError ? error.message : String(error)),
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
          <DialogTitle>Add credential</DialogTitle>
          <DialogDescription>
            The secret is encrypted before it is stored and is never sent back to the browser. Only orchestration
            workers decrypt it when an operation needs it.
          </DialogDescription>
        </DialogHeader>
        <form
          className="space-y-4"
          onSubmit={(e) => {
            e.preventDefault();
            if (valid) submit();
          }}
        >
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label htmlFor="cred-name">Name</Label>
              <Input id="cred-name" value={name} onChange={(e) => setName(e.target.value)} placeholder="lab-ssh-key" aria-invalid={Boolean(nameError)} />
              {nameError && <p className="text-xs text-red-400">{nameError}</p>}
            </div>
            <div className="space-y-1.5">
              <Label>Type</Label>
              <Select value={kind} onValueChange={(v) => setKind(v as CredentialKind)}>
                <SelectTrigger className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {CREDENTIAL_KINDS.map((k) => (
                    <SelectItem key={k} value={k}>
                      {humanize(k)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="cred-secret">Secret value</Label>
            <Textarea
              id="cred-secret"
              value={secret}
              onChange={(e) => setSecret(e.target.value)}
              placeholder={SECRET_HINT[kind]}
              className="min-h-32 font-mono text-xs"
              autoComplete="off"
              spellCheck={false}
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="cred-description">Description (optional)</Label>
            <Input id="cred-description" value={description} onChange={(e) => setDescription(e.target.value)} />
          </div>
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <Label>Non-secret metadata (optional)</Label>
              <Button type="button" variant="ghost" size="xs" onClick={() => setMetadata((m) => [...m, { key: "", value: "" }])}>
                <Plus /> Add field
              </Button>
            </div>
            {metadata.map((row, index) => (
              <div key={index} className="flex gap-2">
                <Input
                  placeholder="username"
                  value={row.key}
                  onChange={(e) => setMetadata((m) => m.map((r, i) => (i === index ? { ...r, key: e.target.value } : r)))}
                />
                <Input
                  placeholder="value"
                  value={row.value}
                  onChange={(e) => setMetadata((m) => m.map((r, i) => (i === index ? { ...r, value: e.target.value } : r)))}
                />
                <Button type="button" variant="ghost" size="icon" aria-label="Remove field" onClick={() => setMetadata((m) => m.filter((_, i) => i !== index))}>
                  <Trash2 />
                </Button>
              </div>
            ))}
            <p className="text-xs text-muted-foreground">Metadata is stored unencrypted and shown in the UI — never put secrets here.</p>
          </div>
          {serverError && <p role="alert" className="rounded border border-red-500/30 bg-red-500/5 p-2 text-sm text-red-300">{serverError}</p>}
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={!valid || create.isPending}>
              <Lock /> {create.isPending ? "Encrypting…" : "Store credential"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export function CredentialsPage() {
  const credentials = useCredentials();
  const remove = useDeleteCredential();
  const [creating, setCreating] = useState(false);
  const [deleting, setDeleting] = useState<Credential | null>(null);

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow="Security"
        title="Credentials"
        description="SSH keys, kubeconfigs and cloud credentials used by the platform. Values are encrypted at rest, write-only through the API, and never exposed to the browser."
        actions={
          <Button size="sm" onClick={() => setCreating(true)}>
            <Plus /> Add credential
          </Button>
        }
      />
      <Card className="py-0">
        <CardContent className="px-0">
          {credentials.isLoading && <div className="p-4"><TableSkeleton rows={4} columns={5} /></div>}
          {credentials.isError && <div className="p-4"><ErrorState error={credentials.error} onRetry={() => credentials.refetch()} /></div>}
          {credentials.data?.items.length === 0 && (
            <EmptyState
              className="m-4"
              icon={KeyRound}
              title="No credentials stored"
              description="Add the SSH keys, kubeconfigs or cloud credentials the platform will use to reach your infrastructure and clusters."
              action={<Button variant="outline" size="sm" onClick={() => setCreating(true)}><Plus /> Add credential</Button>}
            />
          )}
          {credentials.data && credentials.data.items.length > 0 && (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Name</TableHead>
                  <TableHead>Type</TableHead>
                  <TableHead>Metadata</TableHead>
                  <TableHead>Encryption key</TableHead>
                  <TableHead>Created</TableHead>
                  <TableHead>Last used</TableHead>
                  <TableHead className="w-12" />
                </TableRow>
              </TableHeader>
              <TableBody>
                {credentials.data.items.map((c) => (
                  <TableRow key={c.id}>
                    <TableCell>
                      <p className="font-medium">{c.name}</p>
                      {c.description && <p className="text-xs text-muted-foreground">{c.description}</p>}
                    </TableCell>
                    <TableCell>
                      <Badge variant="secondary" className="font-normal">{humanize(c.kind)}</Badge>
                    </TableCell>
                    <TableCell className="max-w-56">
                      <div className="flex flex-wrap gap-1">
                        {Object.entries(c.public_metadata).map(([k, v]) => (
                          <span key={k} className="rounded bg-muted px-1.5 font-mono text-[11px]">{k}={v}</span>
                        ))}
                        {Object.keys(c.public_metadata).length === 0 && <span className="text-muted-foreground">—</span>}
                      </div>
                    </TableCell>
                    <TableCell className="font-mono text-xs text-muted-foreground">{c.secret_backend}:{c.key_id}</TableCell>
                    <TableCell className="text-sm text-muted-foreground">{formatRelative(c.created_at)}</TableCell>
                    <TableCell className="text-sm text-muted-foreground">{c.last_used_at ? formatRelative(c.last_used_at) : "Never"}</TableCell>
                    <TableCell>
                      <Button variant="ghost" size="icon" aria-label={`Delete ${c.name}`} onClick={() => setDeleting(c)}>
                        <Trash2 className="text-red-400" />
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      <CreateCredentialDialog open={creating} onOpenChange={setCreating} />
      <ConfirmDialog
        open={deleting !== null}
        onOpenChange={(open) => !open && setDeleting(null)}
        title={`Delete credential "${deleting?.name}"?`}
        description={
          <>
            <p>The encrypted secret is permanently destroyed. Anything that relies on it will lose access.</p>
            <p>Credentials still referenced by providers, machines or clusters cannot be deleted.</p>
          </>
        }
        confirmText={deleting?.name}
        confirmLabel="Delete credential"
        pending={remove.isPending}
        onConfirm={() =>
          deleting &&
          remove.mutate(
            { id: deleting.id, confirm: deleting.name },
            {
              onSuccess: () => {
                toast.success(`Credential "${deleting.name}" deleted`);
                setDeleting(null);
              },
              onError: (error) => {
                const usedBy = error instanceof ApiError ? (error.details.used_by as string[] | undefined) : undefined;
                toast.error("Could not delete credential", {
                  description: usedBy?.length ? `In use by ${usedBy.join(", ")}` : error.message,
                });
              },
            },
          )
        }
      />
    </div>
  );
}
