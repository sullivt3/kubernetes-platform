"use client";

import { useMutation } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { apiRequest } from "@/lib/api/client";
import type { ClusterDetail, NodePoolInput, OperationDetail } from "@/lib/api/types";

function Pool({ cluster, pool }: { cluster: ClusterDetail; pool: NodePoolInput }) {
  const router = useRouter();
  const [count, setCount] = useState(pool.count);
  const [confirm, setConfirm] = useState("");
  const shrinking = count < pool.count;
  const resize = useMutation({
    mutationFn: () => apiRequest<OperationDetail>(`/clusters/${cluster.id}/pools/${encodeURIComponent(pool.name)}/resize`, { method: "POST", body: { count }, query: { confirm: shrinking ? confirm : undefined } }),
    onSuccess: (operation) => router.push(`/operations/${operation.id}`),
    onError: (error) => toast.error("Could not resize worker pool", { description: error.message }),
  });
  return <div className="space-y-3 rounded-md border border-border p-4">
    <p className="font-medium">{pool.name}</p>
    <p className="text-xs text-muted-foreground">Desired: {pool.count}{cluster.distribution === "gke" ? " nodes per zone" : " nodes"}. {cluster.distribution === "k3d" && "Scaling down deletes local node containers and their data."}</p>
    <div className="flex flex-wrap items-end gap-3"><div className="space-y-1"><Label>New desired count</Label><Input type="number" min={0} max={1000} value={count} className="w-28" onChange={(e) => setCount(Number(e.target.value))} /></div>
      {shrinking && <div className="space-y-1"><Label>Type {cluster.name} to confirm node removal</Label><Input value={confirm} onChange={(e) => setConfirm(e.target.value)} autoComplete="off" /></div>}
      <Button onClick={() => resize.mutate()} disabled={!Number.isInteger(count) || count < 0 || count > 1000 || count === pool.count || (shrinking && confirm !== cluster.name) || resize.isPending || Boolean(cluster.active_operation_id)}>{resize.isPending ? "Submitting…" : "Resize pool"}</Button>
    </div>
  </div>;
}

export function WorkerPools({ cluster }: { cluster: ClusterDetail }) {
  if (cluster.origin !== "provisioned" || !["eks", "aks", "gke", "k3d"].includes(cluster.distribution)) return null;
  return <Card><CardContent className="space-y-4"><h2 className="font-semibold">Worker pools</h2>
    <p className="text-sm text-muted-foreground">Scaling runs as a tracked operation. Scaling down removes nodes and may interrupt workloads. Autoscaled pools must be managed through their autoscaler.</p>
    {cluster.spec.worker_pools.map((pool) => <Pool key={`${pool.name}-${pool.count}`} cluster={cluster} pool={pool} />)}
  </CardContent></Card>;
}
