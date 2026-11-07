"use client";

import { Activity, Search } from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import {
  Command,
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { Button } from "@/components/ui/button";
import { useOperations } from "@/lib/api/hooks";
import { NAVIGATION } from "@/lib/navigation";

/**
 * Searches pages and recent operations. Kubernetes resources (pods, nodes, services...)
 * join the index once cluster connectivity exists (Phase 1+).
 */
export function GlobalSearch() {
  const [open, setOpen] = useState(false);
  const router = useRouter();
  const operations = useOperations({ limit: 20 });

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setOpen((value) => !value);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const go = (href: string) => {
    setOpen(false);
    router.push(href);
  };

  return (
    <>
      <Button
        variant="outline"
        className="h-8 w-full max-w-sm justify-start gap-2 text-muted-foreground"
        onClick={() => setOpen(true)}
      >
        <Search className="size-4" />
        <span className="flex-1 text-left text-sm">
          Search pages and operations…
        </span>
        <kbd className="hidden rounded border border-border bg-muted px-1.5 font-mono text-[10px] sm:inline">
          Ctrl K
        </kbd>
      </Button>
      <CommandDialog
        open={open}
        onOpenChange={setOpen}
        title="Search"
        description="Search pages and recent operations"
      >
        <Command>
          <CommandInput placeholder="Type to search…" />
          <CommandList>
            <CommandEmpty>
              No results. Cluster resources become searchable once clusters are
              connected.
            </CommandEmpty>
            {NAVIGATION.map((section) => (
              <CommandGroup key={section.title} heading={section.title}>
                {section.items.map((item) => (
                  <CommandItem
                    key={item.href}
                    value={`${section.title} ${item.title} ${item.keywords?.join(" ") ?? ""}`}
                    onSelect={() => go(item.href)}
                  >
                    <section.icon />
                    <span>{item.title}</span>
                    {item.phase !== undefined && (
                      <span className="ml-auto font-mono text-[10px] text-muted-foreground">
                        Phase {item.phase}
                      </span>
                    )}
                  </CommandItem>
                ))}
              </CommandGroup>
            ))}
            {operations.data && operations.data.items.length > 0 && (
              <CommandGroup heading="Recent operations">
                {operations.data.items.map((op) => (
                  <CommandItem
                    key={op.id}
                    value={`operation ${op.title} ${op.id} ${op.status}`}
                    onSelect={() => go(`/operations/${op.id}`)}
                  >
                    <Activity />
                    <span className="truncate">{op.title}</span>
                    <span className="ml-auto text-xs text-muted-foreground">
                      {op.status}
                    </span>
                  </CommandItem>
                ))}
              </CommandGroup>
            )}
          </CommandList>
        </Command>
      </CommandDialog>
    </>
  );
}
