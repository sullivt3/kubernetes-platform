"use client";

import { ScrollText } from "lucide-react";
import { useState } from "react";

import { EmptyState, ErrorState, PageHeader, TableSkeleton } from "@/components/common/page";
import { StatusPill } from "@/components/common/status";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { useAuditEvents } from "@/lib/api/hooks";
import { formatDateTime } from "@/lib/format";

const RESOURCE_TYPES = ["all", "operation", "credential"];
const PAGE_SIZE = 50;

export function AuditLog() {
  const [resourceType, setResourceType] = useState("all");
  const [page, setPage] = useState(0);
  const events = useAuditEvents({
    resource_type: resourceType === "all" ? undefined : resourceType,
    limit: PAGE_SIZE,
    offset: page * PAGE_SIZE,
  });
  const total = events.data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow="Administration"
        title="Audit log"
        description="Append-only record of every change made through the platform: who, what, when, from where and with what outcome."
      />
      <div className="flex items-center justify-between">
        <Select
          value={resourceType}
          onValueChange={(v) => {
            setResourceType(v);
            setPage(0);
          }}
        >
          <SelectTrigger className="w-48" aria-label="Filter by resource type">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {RESOURCE_TYPES.map((t) => (
              <SelectItem key={t} value={t}>
                {t === "all" ? "All resources" : t[0].toUpperCase() + t.slice(1)}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        {events.data && <p className="text-sm text-muted-foreground">{total} event(s)</p>}
      </div>
      <Card className="py-0">
        <CardContent className="px-0">
          {events.isLoading && <div className="p-4"><TableSkeleton rows={6} columns={5} /></div>}
          {events.isError && <div className="p-4"><ErrorState error={events.error} onRetry={() => events.refetch()} /></div>}
          {events.data?.items.length === 0 && (
            <EmptyState className="m-4" icon={ScrollText} title="No audit events" description="Changes made through the platform will be recorded here." />
          )}
          {events.data && events.data.items.length > 0 && (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Time</TableHead>
                  <TableHead>Actor</TableHead>
                  <TableHead>Action</TableHead>
                  <TableHead>Resource</TableHead>
                  <TableHead>Outcome</TableHead>
                  <TableHead>Source</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {events.data.items.map((e) => (
                  <TableRow key={e.id}>
                    <TableCell className="text-sm whitespace-nowrap text-muted-foreground">{formatDateTime(e.occurred_at)}</TableCell>
                    <TableCell className="max-w-44 truncate font-mono text-xs">{e.actor_subject}</TableCell>
                    <TableCell className="font-mono text-xs">{e.action}</TableCell>
                    <TableCell className="max-w-64">
                      <p className="truncate text-sm">{e.resource_name ?? e.resource_id ?? "—"}</p>
                      <p className="text-xs text-muted-foreground">{e.resource_type}</p>
                    </TableCell>
                    <TableCell>
                      <StatusPill tone={e.outcome === "success" ? "success" : e.outcome === "denied" ? "warning" : "danger"}>
                        {e.outcome}
                      </StatusPill>
                    </TableCell>
                    <TableCell className="font-mono text-[11px] text-muted-foreground">
                      <p>{e.client_ip ?? "—"}</p>
                      <p title="Request ID">{e.request_id?.slice(0, 12) ?? ""}</p>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
      {pages > 1 && (
        <div className="flex items-center justify-end gap-2 text-sm">
          <Button variant="outline" size="sm" disabled={page === 0} onClick={() => setPage((p) => p - 1)}>Previous</Button>
          <span className="text-muted-foreground">Page {page + 1} of {pages}</span>
          <Button variant="outline" size="sm" disabled={page + 1 >= pages} onClick={() => setPage((p) => p + 1)}>Next</Button>
        </div>
      )}
    </div>
  );
}
