"use client";

import Link from "next/link";
import { useState } from "react";

import { ConfirmDialog } from "@/components/common/confirm-dialog";
import { ErrorState, PageHeader } from "@/components/common/page";
import { useCan } from "@/components/providers/auth";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Phase6Operation } from "@/features/kubernetes/phase6-operation";
import { ClusterScope, LiveError } from "@/features/kubernetes/shared";
import { ResourceTable } from "@/features/workloads/resource-list";
import { useCluster } from "@/lib/api/hooks";
import { useNfsAction, useNfsStatus } from "@/lib/api/phase6";

function NfsDriver({ clusterId }: { clusterId: string }) {
  const canWrite = useCan("clusters.write");
  const cluster = useCluster(clusterId);
  const status = useNfsStatus(clusterId);
  const action = useNfsAction(clusterId);
  const [configureClass, setConfigureClass] = useState(true);
  const [className, setClassName] = useState("nfs-csi");
  const [server, setServer] = useState("");
  const [share, setShare] = useState("/exports");
  const [makeDefault, setMakeDefault] = useState(false);
  const [removeOpen, setRemoveOpen] = useState(false);
  const [operationId, setOperationId] = useState<string | null>(null);
  const data = status.data;
  return (
    <section className="space-y-4 rounded-xl border p-5">
      <div className="space-y-1">
        <h2 className="font-semibold">NFS CSI driver (preview) {data?.version}</h2>
        <p className="text-sm text-muted-foreground">Install the upstream NFS controller and node plugins on Linux nodes using /var/lib/kubelet. This driver runs privileged containers with host mounts. A reachable NFS 4.1 server and writable export must already exist.</p>
      </div>
      {status.isLoading && <p className="text-sm text-muted-foreground">Checking the installed driver…</p>}
      {status.isError && <LiveError error={status.error} clusterId={clusterId} onRetry={() => status.refetch()} />}
      {data && <p className="text-sm">{data.installed ? data.ready ? "Ready" : "Installed; waiting for readiness" : "Not installed"} · Controller {data.controller_ready ? "ready" : "not ready"} · Nodes {data.nodes_ready}/{data.nodes_desired}{data.installed && !data.managed ? " · Managed outside this platform" : ""}</p>}
      {canWrite && data && (!data.installed || data.managed) && <form className="space-y-4" onSubmit={(event) => {
        event.preventDefault();
        action.mutate({ install: { storage_class: configureClass ? className : null, server: configureClass ? server : null, share: configureClass ? share : null, make_default: configureClass && makeDefault } }, { onSuccess: (op) => setOperationId(op.id) });
      }}>
        <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={configureClass} onChange={(e) => setConfigureClass(e.target.checked)} />Configure a StorageClass</label>
        {configureClass && <div className="space-y-3">
          <div className="grid gap-3 md:grid-cols-3">
            <div className="space-y-2"><Label htmlFor="nfs-class">StorageClass name</Label><Input id="nfs-class" value={className} onChange={(e) => setClassName(e.target.value)} pattern="[a-z0-9][a-z0-9\-]*" maxLength={63} required /></div>
            <div className="space-y-2"><Label htmlFor="nfs-server">NFS server address</Label><Input id="nfs-server" value={server} onChange={(e) => setServer(e.target.value)} placeholder="nfs.example.internal" required maxLength={253} /></div>
            <div className="space-y-2"><Label htmlFor="nfs-share">Export path</Label><Input id="nfs-share" value={share} onChange={(e) => setShare(e.target.value)} placeholder="/exports" required maxLength={1024} /></div>
          </div>
          <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={makeDefault} onChange={(e) => setMakeDefault(e.target.checked)} />Use as the default StorageClass (requires no other default)</label>
          <p className="text-xs text-muted-foreground">Volumes use the Retain policy and retained NFS subdirectories. Removing the driver is blocked while volumes or claims reference it. Provisioning is verified when a claim binds; driver readiness alone does not validate the NFS export.</p>
        </div>}
        <div className="flex flex-wrap gap-3">
          <Button type="submit" disabled={action.isPending}>{action.isPending ? "Starting…" : data.managed ? "Reconcile NFS CSI" : "Install NFS CSI"}</Button>
          {data.managed && <Button type="button" variant="destructive" disabled={action.isPending || !cluster.data?.name} onClick={() => setRemoveOpen(true)}>Remove driver</Button>}
        </div>
      </form>}
      {data && data.storage_classes.length > 0 && <p className="text-sm text-muted-foreground">NFS StorageClasses: {data.storage_classes.map((c) => c.name).join(", ")}. <Link href="/storage/storage-classes" className="underline underline-offset-4">Manage storage classes</Link></p>}
      {action.isError && <ErrorState error={action.error} />}
      <Phase6Operation id={operationId} />
      <ConfirmDialog open={canWrite && removeOpen} onOpenChange={setRemoveOpen} title="Remove NFS CSI driver?" description={<><p>Remove the platform-managed controller, node plugins, RBAC and StorageClasses. Existing volumes, claims, volume attachments, and resources managed elsewhere block removal. No volume data is deleted.</p>{action.isError && <p className="text-destructive">{action.error.message}</p>}</>} confirmText={cluster.data?.name} confirmLabel="Remove driver" pending={action.isPending} onConfirm={() => {
        if (cluster.data?.name) action.mutate({ remove: cluster.data.name }, { onSuccess: (op) => { setOperationId(op.id); setRemoveOpen(false); } });
      }} />
    </section>
  );
}

export function CsiDriversPage() {
  return <ClusterScope>{(clusterId) => <div className="space-y-6" key={clusterId}>
    <PageHeader eyebrow="Storage" title="CSI drivers" description="Inspect registered drivers and install the supported NFS CSI integration. Other vendor drivers can be managed with YAML." />
    <NfsDriver clusterId={clusterId} />
    <ResourceTable clusterId={clusterId} kind="csidrivers" />
  </div>}</ClusterScope>;
}
