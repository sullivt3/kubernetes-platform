"use client";

import { Plus, Radar, ServerOff } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import { EmptyState, PageHeader, TableSkeleton } from "@/components/common/page";
import { OperationStatusBadge, StatusPill } from "@/components/common/status";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Textarea } from "@/components/ui/textarea";
import { ApiError } from "@/lib/api/client";
import { useOperations } from "@/lib/api/hooks";
import { useNetworkScan, useOperationResult } from "@/lib/api/infra";
import type { NetworkHost } from "@/lib/api/types";
import { formatRelative } from "@/lib/format";
import { cn } from "@/lib/utils";
import { RegisterDialog } from "./machines";

function ScanResults({ operationId }: { operationId: string }) {
  const op = useOperationResult(operationId);
  const [registering, setRegistering] = useState<NetworkHost | null>(null);
  if (!op.data) return <TableSkeleton rows={3} columns={4} />;
  const running = !["succeeded", "failed", "cancelled"].includes(op.data.status);
  const hosts = (op.data.result?.hosts as NetworkHost[] | undefined) ?? [];
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-3 text-sm text-muted-foreground">
        <OperationStatusBadge status={op.data.status} />
        {running && <span>{Math.round(op.data.progress * 100)}% probed</span>}
        {op.data.status === "succeeded" && (
          <span>
            {hosts.length} SSH server{hosts.length === 1 ? "" : "s"} in {String(op.data.result?.scanned ?? 0)} probes
          </span>
        )}
        {op.data.status === "failed" && <span className="text-red-300">{op.data.error?.message}</span>}
      </div>
      {op.data.status === "succeeded" && hosts.length === 0 && (
        <EmptyState icon={ServerOff} title="No SSH servers found" description="Nothing answered with an SSH banner on the scanned addresses and ports." />
      )}
      {hosts.length > 0 && (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Address</TableHead>
              <TableHead>Operating system (from banner)</TableHead>
              <TableHead>SSH banner</TableHead>
              <TableHead className="text-right">Inventory</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {hosts.map((h) => (
              <TableRow key={`${h.address}:${h.port}`}>
                <TableCell>
                  <p className="font-mono text-sm">{h.address}:{h.port}</p>
                  {h.hostname && <p className="text-xs text-muted-foreground">{h.hostname}</p>}
                </TableCell>
                <TableCell className="text-sm">{h.os_hint ?? <span className="text-muted-foreground">Unknown</span>}</TableCell>
                <TableCell className="max-w-80 truncate font-mono text-xs text-muted-foreground" title={h.banner}>{h.banner}</TableCell>
                <TableCell className="text-right">
                  {h.registered_as ? (
                    <StatusPill tone="success">{h.registered_as}</StatusPill>
                  ) : (
                    <Button size="xs" variant="outline" onClick={() => setRegistering(h)}><Plus /> Register</Button>
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
      <RegisterDialog
        open={registering !== null}
        onOpenChange={(open) => !open && setRegistering(null)}
        initial={registering ? { host: registering.address, port: String(registering.port), name: suggestName(registering) } : undefined}
      />
    </div>
  );
}

function suggestName(host: NetworkHost): string {
  const base = (host.hostname?.split(".")[0] ?? `host-${host.address.replace(/[.:]/g, "-")}`).toLowerCase();
  return base.replace(/[^a-z0-9-]/g, "-").replace(/^-+|-+$/g, "").slice(0, 63);
}

export function NetworkDiscoveryPage() {
  const scan = useNetworkScan();
  const recent = useOperations({ type: "network.discover", limit: 10 });
  const [ranges, setRanges] = useState("");
  const [ports, setPorts] = useState("22");
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const rangeList = ranges.split(/[\s,]+/).map((r) => r.trim()).filter(Boolean);
  const portList = ports.split(/[\s,]+/).map(Number).filter((p) => Number.isInteger(p) && p > 0 && p < 65536);
  const current = selected ?? recent.data?.items[0]?.id ?? null;

  const submit = () => {
    setError(null);
    scan.mutate(
      { ranges: rangeList, ports: portList },
      {
        onSuccess: (op) => {
          setSelected(op.id);
          toast.info("Scan started");
        },
        onError: (e) => setError(e instanceof ApiError ? e.message : String(e)),
      },
    );
  };

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow="Infrastructure"
        title="Network discovery"
        description="Find Linux servers by probing address ranges for SSH. Probes only read the SSH greeting; no credentials are sent. Register what you find to inspect it properly."
      />
      <Card>
        <CardContent className="grid gap-4 md:grid-cols-[1fr_12rem_auto] md:items-end">
          <div className="space-y-1.5">
            <Label htmlFor="d-ranges">Address ranges</Label>
            <Textarea
              id="d-ranges"
              rows={2}
              value={ranges}
              onChange={(e) => setRanges(e.target.value)}
              placeholder="192.168.1.0/24, 10.0.0.10-40, 10.0.5.7"
              className="font-mono text-xs"
            />
            <p className="text-xs text-muted-foreground">CIDRs, last-octet ranges or single addresses. Private, loopback and link-local ranges only; up to 8192 probes.</p>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="d-ports">SSH ports</Label>
            <Input id="d-ports" value={ports} onChange={(e) => setPorts(e.target.value)} className="font-mono text-xs" />
            <p className="text-xs text-muted-foreground">Comma-separated</p>
          </div>
          <Button onClick={submit} disabled={!rangeList.length || !portList.length || scan.isPending} className="md:mb-6">
            <Radar /> {scan.isPending ? "Starting…" : "Scan"}
          </Button>
          {error && <p role="alert" className="rounded border border-red-500/30 bg-red-500/5 p-2 text-sm text-red-300 md:col-span-3">{error}</p>}
        </CardContent>
      </Card>

      <div className="grid gap-6 lg:grid-cols-[16rem_1fr]">
        <div className="space-y-2">
          <p className="text-sm font-medium">Recent scans</p>
          {recent.isLoading && <TableSkeleton rows={3} columns={1} />}
          {recent.data?.items.length === 0 && <p className="text-sm text-muted-foreground">No scans yet.</p>}
          <ul className="space-y-1">
            {recent.data?.items.map((op) => (
              <li key={op.id}>
                <button
                  type="button"
                  onClick={() => setSelected(op.id)}
                  className={cn(
                    "w-full rounded-md border px-3 py-2 text-left text-sm transition-colors hover:bg-muted/40",
                    current === op.id ? "border-primary/50 bg-muted/40" : "border-border/60",
                  )}
                >
                  <p className="truncate">{op.title.replace("Network discovery ", "")}</p>
                  <p className="text-xs text-muted-foreground">{formatRelative(op.created_at)}</p>
                </button>
              </li>
            ))}
          </ul>
        </div>
        <div>
          {current ? (
            <ScanResults key={current} operationId={current} />
          ) : (
            <EmptyState icon={Radar} title="No scan selected" description="Run a scan to find servers on your network." />
          )}
        </div>
      </div>
    </div>
  );
}
