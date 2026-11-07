"use client";

import Link from "next/link";

import { OperationStatusBadge } from "@/components/common/status";
import { Progress } from "@/components/ui/progress";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import type { OperationSummary } from "@/lib/api/types";
import { formatDuration, formatRelative, shortId } from "@/lib/format";

export function OperationsTable({ operations, compact = false }: { operations: OperationSummary[]; compact?: boolean }) {
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Operation</TableHead>
          <TableHead>Status</TableHead>
          {!compact && <TableHead>Target</TableHead>}
          <TableHead className="w-36">Progress</TableHead>
          {!compact && <TableHead>Requested by</TableHead>}
          <TableHead>Started</TableHead>
          <TableHead className="text-right">Duration</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {operations.map((op) => (
          <TableRow key={op.id} className="group">
            <TableCell className="max-w-72">
              <Link href={`/operations/${op.id}`} className="block truncate font-medium group-hover:underline">
                {op.title}
              </Link>
              <span className="font-mono text-[11px] text-muted-foreground">
                {op.type} · {shortId(op.id)}
                {op.attempt > 1 && ` · attempt ${op.attempt}`}
              </span>
            </TableCell>
            <TableCell>
              <OperationStatusBadge status={op.status} />
            </TableCell>
            {!compact && (
              <TableCell className="text-sm text-muted-foreground">
                {op.target_name ?? op.target_id ?? "—"}
              </TableCell>
            )}
            <TableCell>
              <div className="flex items-center gap-2">
                <Progress value={op.progress} className="h-1.5" />
                <span className="w-9 text-right text-xs tabular-nums text-muted-foreground">
                  {Math.round(op.progress)}%
                </span>
              </div>
            </TableCell>
            {!compact && (
              <TableCell className="max-w-40 truncate font-mono text-xs text-muted-foreground">
                {op.requested_by_subject}
              </TableCell>
            )}
            <TableCell className="text-sm text-muted-foreground" title={op.started_at ?? op.created_at}>
              {formatRelative(op.started_at ?? op.created_at)}
            </TableCell>
            <TableCell className="text-right text-sm tabular-nums text-muted-foreground">
              {formatDuration(op.started_at, op.finished_at)}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}
