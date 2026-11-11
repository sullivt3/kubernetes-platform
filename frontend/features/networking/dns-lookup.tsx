"use client";

import { useState } from "react";

import { ErrorState } from "@/components/common/page";
import { useCan } from "@/components/providers/auth";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Phase6Operation } from "@/features/kubernetes/phase6-operation";
import { useClusterNamespaces } from "@/lib/api/kube";
import { type DnsLookupInput, useDnsLookup } from "@/lib/api/phase6";

export function DnsLookupPanel({ clusterId }: { clusterId: string }) {
  const canWrite = useCan("clusters.write");
  const namespaces = useClusterNamespaces(clusterId);
  const lookup = useDnsLookup(clusterId);
  const [namespace, setNamespace] = useState("default");
  const [hostname, setHostname] = useState("kubernetes.default.svc");
  const [recordType, setRecordType] = useState<DnsLookupInput["record_type"]>("A");
  const [operationId, setOperationId] = useState<string | null>(null);
  return (
    <section className="space-y-4 rounded-xl border p-5">
      <div className="space-y-1">
        <h2 className="font-semibold">Test DNS resolution</h2>
        <p className="text-sm text-muted-foreground">Run nslookup inside a temporary pod in the selected namespace. Namespace network policies apply. The job runs for at most 60 seconds and is removed after collecting its output.</p>
      </div>
      <form className="flex flex-wrap items-end gap-3" onSubmit={(event) => {
        event.preventDefault();
        lookup.mutate({ namespace, hostname, record_type: recordType }, { onSuccess: (op) => setOperationId(op.id) });
      }}>
        <div className="space-y-2">
          <Label htmlFor="dns-namespace">Namespace</Label>
          <Select value={namespace} onValueChange={setNamespace}>
            <SelectTrigger id="dns-namespace" className="w-48"><SelectValue /></SelectTrigger>
            <SelectContent>{[...new Set(["default", ...(namespaces.data ?? []).map((n) => n.name)])].map((name) => <SelectItem key={name} value={name}>{name}</SelectItem>)}</SelectContent>
          </Select>
        </div>
        <div className="min-w-64 flex-1 space-y-2">
          <Label htmlFor="dns-hostname">DNS name</Label>
          <Input id="dns-hostname" value={hostname} onChange={(e) => setHostname(e.target.value)} required maxLength={253} pattern="[A-Za-z0-9_][A-Za-z0-9_.\-]*" />
        </div>
        <div className="space-y-2">
          <Label htmlFor="dns-type">Record type</Label>
          <Select value={recordType} onValueChange={(value) => setRecordType(value as DnsLookupInput["record_type"])}>
            <SelectTrigger id="dns-type" className="w-28"><SelectValue /></SelectTrigger>
            <SelectContent>{["A", "AAAA", "CNAME", "SRV", "TXT", "MX"].map((type) => <SelectItem key={type} value={type}>{type}</SelectItem>)}</SelectContent>
          </Select>
        </div>
        <Button type="submit" disabled={!canWrite || lookup.isPending || !hostname}>{lookup.isPending ? "Starting…" : "Run lookup"}</Button>
      </form>
      {namespaces.isError && <ErrorState error={namespaces.error} onRetry={() => namespaces.refetch()} />}
      {lookup.isError && <ErrorState error={lookup.error} />}
      <Phase6Operation id={operationId} />
    </section>
  );
}
