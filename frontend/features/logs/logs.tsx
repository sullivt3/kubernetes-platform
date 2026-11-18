"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, ChevronsUpDown, Search, Terminal } from "lucide-react";
import { useState } from "react";

import { EmptyState, ErrorState, PageHeader, TableSkeleton } from "@/components/common/page";
import { useCan } from "@/components/providers/auth";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Command, CommandEmpty, CommandInput, CommandItem, CommandList } from "@/components/ui/command";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Input } from "@/components/ui/input";
import { ClusterScope, LiveError } from "@/features/kubernetes/shared";
import { api, apiRequest } from "@/lib/api/client";

type Connection = { namespace: string; service: string; port: string };
type Settings = { connection: Connection | null };
type Filters = { namespace: string; pod: string; container: string; contains: string; window: string };
type Entry = {
  timestamp_ns: string;
  namespace: string;
  pod: string | null;
  container: string | null;
  line: string;
  truncated: boolean;
};
type Results = { entries: Entry[]; limited: boolean; limit: number };

const timeFormat = new Intl.DateTimeFormat(undefined, {
  month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit", fractionalSecondDigits: 3,
});

function ConnectionForm({ clusterId, connection, onClose }: {
  clusterId: string; connection: Connection | null; onClose: () => void;
}) {
  const [values, setValues] = useState(connection ?? { namespace: "", service: "", port: "3100" });
  const client = useQueryClient();
  const save = useMutation({
    mutationFn: (next: Connection | null) => api.put<Settings>(`/clusters/${clusterId}/logs/connection`, { connection: next }),
    onSuccess: (settings) => {
      client.setQueryData(["log-search", clusterId, "connection"], settings);
      void client.invalidateQueries({ queryKey: ["log-search", clusterId, "results"] });
      onClose();
    },
  });
  return (
    <Card>
      <CardHeader><CardTitle>Loki connection</CardTitle></CardHeader>
      <CardContent>
        <form className="space-y-4" onSubmit={(event) => { event.preventDefault(); save.mutate(values); }}>
          <p className="text-sm text-muted-foreground">Enter the Kubernetes Service that serves your Loki HTTP API.</p>
          <div className="grid gap-3 sm:grid-cols-3">
            {(["namespace", "service", "port"] as const).map((field) => (
              <label key={field} className="block space-y-1 text-sm">
                {{ namespace: "Namespace", service: "Service", port: "Service port" }[field]}
                <Input required maxLength={field === "port" ? 15 : 63} value={values[field]}
                  onChange={(event) => setValues({ ...values, [field]: event.target.value.trim() })} />
              </label>
            ))}
          </div>
          {save.isError && <ErrorState error={save.error} />}
          <div className="flex flex-wrap gap-2">
            <Button disabled={save.isPending}>Save connection</Button>
            <Button type="button" variant="outline" onClick={onClose}>Cancel</Button>
            {connection && <Button type="button" variant="ghost" disabled={save.isPending} onClick={() => save.mutate(null)}>Disconnect</Button>}
          </div>
        </form>
      </CardContent>
    </Card>
  );
}

function LogFilter({ label, value, options, onChange, disabled, loading, optional = false }: {
  label: string; value: string; options: string[]; onChange: (value: string) => void;
  disabled?: boolean; loading: boolean; optional?: boolean;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="space-y-1 text-sm">
      <span>{label}</span>
      <Popover open={open} onOpenChange={setOpen}>
        <PopoverTrigger asChild>
          <Button type="button" variant="outline" role="combobox" aria-label={label} aria-expanded={open}
            disabled={disabled} className="w-full justify-between">
            <span className="truncate">{value || (loading ? "Loading…" : optional ? `All ${label.toLowerCase()}s` : "Select namespace")}</span>
            <ChevronsUpDown className="size-4 shrink-0" />
          </Button>
        </PopoverTrigger>
        <PopoverContent className="w-[var(--radix-popover-trigger-width)] p-0" align="start">
          <Command>
            <CommandInput aria-label={`Search ${label.toLowerCase()}s`} placeholder={`Search ${label.toLowerCase()}s…`} />
            <CommandList>
              <CommandEmpty>{loading ? "Loading…" : "No matching options"}</CommandEmpty>
              {optional && <CommandItem value={`All ${label.toLowerCase()}s`} onSelect={() => { onChange(""); setOpen(false); }}>
                All {label.toLowerCase()}s
              </CommandItem>}
              {options.map((option) => <CommandItem key={option} value={option} onSelect={() => { onChange(option); setOpen(false); }}>
                <span className="truncate">{option}</span>
                {value === option && <Check className="ml-auto size-4" />}
              </CommandItem>)}
            </CommandList>
          </Command>
        </PopoverContent>
      </Popover>
    </div>
  );
}

function SearchLogs({ clusterId, connection }: { clusterId: string; connection: Connection }) {
  const [draft, setDraft] = useState<Filters>({ namespace: "", pod: "", container: "", contains: "", window: "15m" });
  const [submitted, setSubmitted] = useState<Filters | null>(null);
  const namespaces = useQuery({
    queryKey: ["log-search", clusterId, "namespaces", connection, draft.window],
    queryFn: ({ signal }) => apiRequest<string[]>(`/clusters/${clusterId}/logs/namespaces`, {
      query: { window: draft.window }, signal,
    }),
    retry: false,
  });
  const options = namespaces.data ?? [];
  const namespace = options.includes(draft.namespace) ? draft.namespace : options.length === 1 ? options[0] : "";
  const pods = useQuery({
    queryKey: ["log-search", clusterId, "pods", connection, draft.window, namespace],
    queryFn: ({ signal }) => apiRequest<string[]>(`/clusters/${clusterId}/logs/pods`, {
      query: { namespace, window: draft.window }, signal,
    }),
    enabled: Boolean(namespace),
    retry: false,
  });
  const pod = (pods.data ?? []).includes(draft.pod) ? draft.pod : "";
  const containers = useQuery({
    queryKey: ["log-search", clusterId, "containers", connection, draft.window, namespace, pod],
    queryFn: ({ signal }) => apiRequest<string[]>(`/clusters/${clusterId}/logs/containers`, {
      query: { namespace, pod: pod || undefined, window: draft.window }, signal,
    }),
    enabled: Boolean(namespace),
    retry: false,
  });
  const container = (containers.data ?? []).includes(draft.container) ? draft.container : "";
  const result = useQuery({
    queryKey: ["log-search", clusterId, "results", connection, submitted],
    queryFn: ({ signal }) => apiRequest<Results>(`/clusters/${clusterId}/logs`, {
      query: submitted && {
        namespace: submitted.namespace, pod: submitted.pod || undefined, container: submitted.container || undefined,
        contains: submitted.contains || undefined, window: submitted.window,
      } || undefined,
      signal,
    }),
    enabled: submitted !== null,
    retry: false,
    refetchOnWindowFocus: false,
    gcTime: 0,
  });
  return (
    <div className="space-y-4">
      <form className="space-y-3 rounded-lg border p-4" onSubmit={(event) => {
        event.preventDefault();
        if (!namespace || namespaces.isError || result.isFetching) return;
        const filters = { ...draft, namespace, pod, container };
        if (submitted && JSON.stringify(filters) === JSON.stringify(submitted)) void result.refetch();
        else setSubmitted(filters);
      }}>
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
          <LogFilter label="Namespace" value={namespace} options={options} loading={namespaces.isPending}
            disabled={namespaces.isPending || namespaces.isError}
            onChange={(value) => setDraft({ ...draft, namespace: value, pod: "", container: "" })} />
          <LogFilter label="Pod" value={pod} options={pods.data ?? []} loading={pods.isFetching} optional
            disabled={!namespace || pods.isError}
            onChange={(value) => setDraft({ ...draft, pod: value, container: "" })} />
          <LogFilter label="Container" value={container} options={containers.data ?? []} loading={containers.isFetching} optional
            disabled={!namespace || containers.isError}
            onChange={(value) => setDraft({ ...draft, container: value })} />
          <label className="block space-y-1 text-sm">Contains text
            <Input maxLength={160} value={draft.contains} placeholder="Case sensitive"
              onChange={(event) => setDraft({ ...draft, contains: event.target.value })} />
          </label>
          <label className="block space-y-1 text-sm">Time range
            <select className="block h-9 w-full rounded-md border bg-background px-2" value={draft.window}
              onChange={(event) => setDraft({ ...draft, window: event.target.value })}>
              <option value="15m">Last 15 minutes</option>
              <option value="1h">Last hour</option>
              <option value="6h">Last 6 hours</option>
            </select>
          </label>
        </div>
        <Button disabled={!namespace || namespaces.isError || result.isFetching}><Search /> Search logs</Button>
      </form>
      {namespaces.isError && <LiveError error={namespaces.error} clusterId={clusterId} onRetry={() => namespaces.refetch()} />}
      {pods.isError && <LiveError error={pods.error} clusterId={clusterId} onRetry={() => pods.refetch()} />}
      {containers.isError && <LiveError error={containers.error} clusterId={clusterId} onRetry={() => containers.refetch()} />}
      {result.isFetching && <TableSkeleton rows={4} columns={3} />}
      {result.isError && <LiveError error={result.error} clusterId={clusterId} onRetry={() => result.refetch()} />}
      {result.data && !result.isFetching && !result.isError && (
        result.data.entries.length ? (
          <section aria-label="Log results" className="space-y-2">
            <p role="status" className="text-sm text-muted-foreground">
              {result.data.limited
                ? `Showing the newest ${result.data.limit} entries. Narrow the filters to see a smaller result.`
                : `${result.data.entries.length} log ${result.data.entries.length === 1 ? "entry" : "entries"}`}
            </p>
            <ol className="divide-y rounded-lg border">
              {result.data.entries.map((entry, index) => {
                const date = new Date(Number(entry.timestamp_ns.slice(0, -6)));
                return (
                  <li key={`${entry.timestamp_ns}-${index}`} className="space-y-1.5 p-3">
                    <p className="flex flex-wrap gap-x-3 gap-y-0.5 text-xs text-muted-foreground">
                      <time dateTime={date.toISOString()} title={date.toISOString()}>{timeFormat.format(date)}</time>
                      <span className="font-mono break-all">{[entry.namespace, entry.pod, entry.container].filter(Boolean).join("/")}</span>
                      {entry.truncated && <span>Line shortened</span>}
                    </p>
                    <pre className="whitespace-pre-wrap break-words font-mono text-xs leading-relaxed">{entry.line}</pre>
                  </li>
                );
              })}
            </ol>
          </section>
        ) : <EmptyState icon={Search} title="No matching logs" description="Change the filters or select a wider time range." />
      )}
    </div>
  );
}

function ClusterLogs({ clusterId }: { clusterId: string }) {
  const canConfigure = useCan("clusters.write");
  const [editing, setEditing] = useState(false);
  const settings = useQuery({
    queryKey: ["log-search", clusterId, "connection"],
    queryFn: () => api.get<Settings>(`/clusters/${clusterId}/logs/connection`),
  });
  if (settings.isError) return <LiveError error={settings.error} clusterId={clusterId} onRetry={() => settings.refetch()} />;
  if (!settings.data) return <TableSkeleton />;
  const { connection } = settings.data;
  return (
    <div className="space-y-4">
      {editing && canConfigure && <ConnectionForm clusterId={clusterId} connection={connection} onClose={() => setEditing(false)} />}
      {connection ? (
        <>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="text-sm text-muted-foreground">Loki <code>{connection.namespace}/{connection.service}:{connection.port}</code></p>
            {canConfigure && !editing && <Button variant="outline" onClick={() => setEditing(true)}>Edit connection</Button>}
          </div>
          <SearchLogs clusterId={clusterId} connection={connection} />
        </>
      ) : !editing && (
        <EmptyState icon={Terminal} title="Loki connection required" description="Connect a Loki Service to search historical pod logs."
          action={canConfigure && <Button onClick={() => setEditing(true)}>Connect Loki</Button>} />
      )}
    </div>
  );
}

export function LogsPage() {
  return (
    <div className="space-y-5">
      <PageHeader eyebrow="Operations" title="Logs" description="Search historical Kubernetes pod logs collected by Loki." />
      <ClusterScope>{(clusterId) => <ClusterLogs key={clusterId} clusterId={clusterId} />}</ClusterScope>
    </div>
  );
}
