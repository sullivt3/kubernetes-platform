"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";

import { ErrorState } from "@/components/common/page";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api/client";

type Collection = { managed: boolean; selected: string[]; available: string[]; truncated: boolean };

function Form({ id, settings, refresh }: { id: string; settings: Collection; refresh: () => void }) {
  const [selected, setSelected] = useState(settings.selected);
  const mutation = useMutation({
    mutationFn: () => api.post<{ id: string }>(`/clusters/${id}/logs/collection`, { namespaces: selected }),
  });
  return <div className="space-y-3">
    {settings.truncated && <p>Namespace inventory is truncated.</p>}
    <div className="grid max-h-64 gap-2 overflow-auto sm:grid-cols-2">
      {settings.available.map((name) => <label key={name} className="flex items-center gap-2 text-sm">
        <input type="checkbox" checked={selected.includes(name)} onChange={(e) =>
          setSelected(e.target.checked ? [...selected, name] : selected.filter((item) => item !== name))} />{name}
      </label>)}
    </div>
    <p className="text-xs text-muted-foreground">Changing collection restarts the collector. Stored logs remain searchable for their retention period.</p>
    <Button disabled={!selected.length || selected.length > 30 || mutation.isPending} onClick={() => mutation.mutate()}>Update collection</Button>
    <Button variant="outline" onClick={refresh}>Refresh status</Button>
    {mutation.isError && <ErrorState error={mutation.error} />}
    {mutation.data && <Link className="block underline" href={`/operations/${mutation.data.id}`}>View collection operation</Link>}
  </div>;
}

export function CollectionControls({ id }: { id: string }) {
  const [open, setOpen] = useState(false);
  const query = useQuery({
    queryKey: ["log-collection", id], queryFn: () => api.get<Collection>(`/clusters/${id}/logs/collection`), enabled: open,
  });
  return <details onToggle={(e) => setOpen(e.currentTarget.open)} className="rounded-lg border p-4">
    <summary className="cursor-pointer font-medium">Collected namespaces</summary>
    <div className="mt-3">
      {query.isError && <ErrorState error={query.error} onRetry={() => query.refetch()} />}
      {query.isFetching && <p>Loading collection settings…</p>}
      {query.data && (query.data.managed
        ? <Form key={JSON.stringify(query.data.selected)} id={id} settings={query.data} refresh={() => { void query.refetch(); }} />
        : <p>Register an Alloy installation for managed collection to configure namespaces here.</p>)}
    </div>
  </details>;
}
