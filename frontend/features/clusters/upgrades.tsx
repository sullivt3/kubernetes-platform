"use client";

import { ArrowUpCircle, Boxes } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { toast } from "sonner";

import { EmptyState, ErrorState, PageHeader, TableSkeleton } from "@/components/common/page";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useClusters, useKubernetesVersions, useUpgradeCluster } from "@/lib/api/hooks";

function minorOf(version: string | null): number | null {
  const match = version?.replace(/^v/, "").match(/^1\.(\d+)(?:\.(\d+))?(?:[-+].*)?$/);
  return match ? Number(match[1]) : null;
}

export function ClusterUpgrades() {
  const router = useRouter();
  const clusters = useClusters();
  const versions = useKubernetesVersions();
  const upgrade = useUpgradeCluster();
  const [clusterId, setClusterId] = useState("");
  const [targetVersion, setTargetVersion] = useState("");
  const [allowEmptydirData, setAllowEmptydirData] = useState(false);
  const [releaseRevision, setReleaseRevision] = useState(1);

  const upgradeable = (clusters.data ?? []).filter(
    (cluster) =>
      cluster.origin === "provisioned" &&
      ["kubeadm", "k3s", "rke2", "eks", "aks", "gke"].includes(cluster.distribution) &&
      minorOf(cluster.kubernetes_version) !== null &&
      !cluster.active_operation_id &&
      ["active", "degraded"].includes(cluster.lifecycle_state),
  );
  const selected = upgradeable.find((cluster) => cluster.id === clusterId);
  const currentMinor = minorOf(selected?.kubernetes_version ?? null);
  const eks = selected?.distribution === "eks";
  const managed = ["eks", "aks", "gke"].includes(selected?.distribution ?? "");
  const native = ["k3s", "rke2"].includes(selected?.distribution ?? "");
  const targetVersionValid = (eks ? /^1\.\d{1,2}$/ : /^1\.\d{1,2}\.\d{1,3}$/).test(targetVersion);
  const targets = (versions.data?.versions ?? [])
    .filter((version) => {
      const minor = Number(version.minor.split(".")[1]);
      return currentMinor !== null && minor >= currentMinor && minor <= currentMinor + 1;
    })
    .map((version) => eks ? version.minor : version.latest_patch)
    .filter((version) => {
      const [major, minor, patch] = version.split(".").map(Number);
      const current = (selected?.kubernetes_version ?? "").replace(/^v/, "").replace(/[+-].*$/, "").split(".").map(Number);
      return major === 1 && (minor > current[1] || (minor === current[1] && patch > current[2]));
    });

  function chooseCluster(id: string) {
    setClusterId(id);
    setTargetVersion("");
  }

  function submit() {
    if (!selected || !targetVersion) return;
    upgrade.mutate(
      { id: selected.id, targetVersion, allowEmptydirData, targetReleaseRevision: native ? releaseRevision : undefined },
      {
        onSuccess: (operation) => {
          toast.success(`Upgrade to ${targetVersion} queued`);
          router.push(`/operations/${operation.id}`);
        },
        onError: (error) => toast.error(error.message),
      },
    );
  }

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow="Kubernetes"
        title="Cluster upgrades"
        description="Upgrade platform-created clusters one minor release at a time. Self-managed nodes are drained and upgraded in order; managed services use the cloud provider upgrade process."
      />
      {(clusters.isLoading || versions.isLoading) && <TableSkeleton rows={2} columns={3} />}
      {clusters.isError && <ErrorState error={clusters.error} onRetry={() => clusters.refetch()} />}
      {versions.isError && <ErrorState error={versions.error} onRetry={() => versions.refetch()} />}
      {clusters.data && upgradeable.length === 0 && (
        <EmptyState
          icon={Boxes}
          title="No upgradeable clusters"
          description="Create a supported cluster and record its Kubernetes version to enable upgrades."
          action={<Button asChild variant="outline" size="sm"><Link href="/kubernetes/create">Create a cluster</Link></Button>}
        />
      )}
      {clusters.data && upgradeable.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2"><ArrowUpCircle className="size-4" /> Upgrade a cluster</CardTitle>
            <CardDescription>Self-managed control planes upgrade serially, followed by workers. Cloud services upgrade their control plane and node pools through native APIs.</CardDescription>
          </CardHeader>
          <CardContent className="space-y-5">
            <div className="grid gap-4 sm:grid-cols-2">
              <div className="space-y-2">
                <Label htmlFor="upgrade-cluster">Cluster</Label>
                <Select value={clusterId} onValueChange={chooseCluster}>
                  <SelectTrigger id="upgrade-cluster" className="w-full"><SelectValue placeholder="Select a cluster" /></SelectTrigger>
                  <SelectContent>
                    {upgradeable.map((cluster) => <SelectItem key={cluster.id} value={cluster.id}>{cluster.name} · {cluster.kubernetes_version}</SelectItem>)}
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-2">
                <Label htmlFor="upgrade-version">Target version</Label>
                <Input
                  id="upgrade-version"
                  list="upgrade-version-options"
                  value={targetVersion}
                  onChange={(event) => setTargetVersion(event.target.value)}
                  placeholder="1.34.2"
                  maxLength={16}
                  disabled={!selected}
                  aria-describedby="upgrade-version-help"
                />
                <datalist id="upgrade-version-options">{targets.map((version) => <option key={version} value={version} />)}</datalist>
              </div>
            </div>
            {selected && <p id="upgrade-version-help" className="text-xs text-muted-foreground">Current: <span className="font-mono">{selected.kubernetes_version}</span>. {eks ? "EKS accepts the next minor version, for example 1.35." : "Choose an exact patch in the same or next minor release."} Suggested releases come from Kubernetes upstream; confirm that the target is supported by your distribution or cloud service.</p>}
            {native && <div className="space-y-1"><Label>Target distribution release revision</Label><Input type="number" min={1} max={100} value={releaseRevision} onChange={(e) => setReleaseRevision(Number(e.target.value))} className="w-24" /><p className="text-xs text-muted-foreground">For +k3s1 or +rke2r1, use 1. Select a revision published for the target patch.</p></div>}
            {!managed && <label className="flex items-start gap-2 text-sm text-muted-foreground">
              <input
                type="checkbox"
                checked={allowEmptydirData}
                onChange={(event) => setAllowEmptydirData(event.target.checked)}
                className="mt-0.5 accent-primary"
              />
              <span>Allow the upgrade to evict pods with <code>emptyDir</code> volumes. Data in those temporary volumes will be lost.</span>
            </label>}
            {!versions.data?.available && versions.data?.message && <p role="status" className="text-sm text-amber-300">{versions.data.message}</p>}
            <Button onClick={submit} disabled={!selected || !targetVersionValid || upgrade.isPending}>
              <ArrowUpCircle /> {upgrade.isPending ? "Starting upgrade…" : "Start upgrade"}
            </Button>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
