"use client";

import { Workflow } from "lucide-react";
import { useState } from "react";

import { EmptyState, ErrorState, PageHeader, TableSkeleton } from "@/components/common/page";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useOperations } from "@/lib/api/hooks";
import type { OperationStatus } from "@/lib/api/types";
import { OperationsTable } from "./operations-table";
import { SelfCheckButton } from "./self-check-button";

const FILTERS: { value: string; label: string; status?: OperationStatus[]; active?: boolean }[] = [
  { value: "all", label: "All operations" },
  { value: "active", label: "Active", active: true },
  { value: "succeeded", label: "Succeeded", status: ["succeeded"] },
  { value: "failed", label: "Failed", status: ["failed"] },
  { value: "cancelled", label: "Cancelled", status: ["cancelled"] },
];
const PAGE_SIZE = 25;

export function OperationsList() {
  const [filter, setFilter] = useState("all");
  const [page, setPage] = useState(0);
  const selected = FILTERS.find((f) => f.value === filter) ?? FILTERS[0];
  const operations = useOperations({
    status: selected.status,
    active: selected.active,
    limit: PAGE_SIZE,
    offset: page * PAGE_SIZE,
  });
  const total = operations.data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow="Operations"
        title="Operations"
        description="Every long-running action — provisioning, installation, upgrades, diagnostics — runs as a tracked, recoverable operation with persistent logs."
        actions={<SelfCheckButton />}
      />
      <div className="flex items-center justify-between gap-3">
        <Select
          value={filter}
          onValueChange={(value) => {
            setFilter(value);
            setPage(0);
          }}
        >
          <SelectTrigger className="w-48" aria-label="Filter operations">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {FILTERS.map((f) => (
              <SelectItem key={f.value} value={f.value}>
                {f.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        {operations.data && <p className="text-sm text-muted-foreground">{total} operation(s)</p>}
      </div>
      <Card className="py-0">
        <CardContent className="px-0">
          {operations.isLoading && (
            <div className="p-4">
              <TableSkeleton rows={6} columns={6} />
            </div>
          )}
          {operations.isError && (
            <div className="p-4">
              <ErrorState error={operations.error} onRetry={() => operations.refetch()} />
            </div>
          )}
          {operations.data?.items.length === 0 && (
            <EmptyState
              className="m-4"
              icon={Workflow}
              title={filter === "all" ? "No operations yet" : "No matching operations"}
              description={
                filter === "all"
                  ? "Operations appear here as soon as work is requested. Run the self-check to try the engine."
                  : "Try a different filter."
              }
            />
          )}
          {operations.data && operations.data.items.length > 0 && <OperationsTable operations={operations.data.items} />}
        </CardContent>
      </Card>
      {pages > 1 && (
        <div className="flex items-center justify-end gap-2 text-sm">
          <Button variant="outline" size="sm" disabled={page === 0} onClick={() => setPage((p) => p - 1)}>
            Previous
          </Button>
          <span className="text-muted-foreground">
            Page {page + 1} of {pages}
          </span>
          <Button variant="outline" size="sm" disabled={page + 1 >= pages} onClick={() => setPage((p) => p + 1)}>
            Next
          </Button>
        </div>
      )}
    </div>
  );
}
