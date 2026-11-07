"use client";

import { Activity, Boxes, Check, ChevronsUpDown, Menu, Radio, UserRound } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { StatusDot } from "@/components/common/status";
import { useLiveUpdates } from "@/components/providers/live-updates";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Progress } from "@/components/ui/progress";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useClusters, useOperations, useSystemInfo } from "@/lib/api/hooks";
import { useSelectedCluster } from "./cluster-context";
import { GlobalSearch } from "./global-search";
import { SidebarContent } from "./sidebar";

function ClusterSelector() {
  const clusters = useClusters();
  const { clusterId, setClusterId } = useSelectedCluster();
  const items = clusters.data ?? [];
  const selected = items.find((c) => c.id === clusterId);

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="outline" className="h-8 min-w-44 justify-between gap-2">
          <span className="flex items-center gap-2 truncate">
            <Boxes className="size-4 text-muted-foreground" />
            <span className="truncate">
              {clusters.isLoading ? "Loading clusters…" : selected ? selected.name : items.length ? "Select cluster" : "No clusters"}
            </span>
          </span>
          <ChevronsUpDown className="size-3.5 opacity-60" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="w-64">
        <DropdownMenuLabel>Clusters</DropdownMenuLabel>
        <DropdownMenuSeparator />
        {clusters.isError && <p className="px-2 py-2 text-xs text-red-400">Could not load clusters.</p>}
        {!clusters.isError && items.length === 0 && (
          <div className="space-y-2 px-2 py-2 text-xs text-muted-foreground">
            <p>No clusters yet.</p>
            <Link href="/kubernetes/create" className="text-sky-400 hover:underline">
              Create a cluster
            </Link>
          </div>
        )}
        {items.map((cluster) => (
          <DropdownMenuItem key={cluster.id} onSelect={() => setClusterId(cluster.id)}>
            <span className="flex-1 truncate">{cluster.name}</span>
            {cluster.id === clusterId && <Check className="size-4" />}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function ActiveOperations() {
  const active = useOperations({ active: true, limit: 10 });
  const count = active.data?.total ?? 0;
  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button variant="ghost" size="sm" className="gap-2" aria-label={`${count} active operations`}>
          <Activity className="size-4" />
          <span className="hidden sm:inline">Operations</span>
          {count > 0 && (
            <span className="rounded-full bg-sky-500/20 px-1.5 text-[11px] font-semibold text-sky-300">{count}</span>
          )}
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-80 p-0">
        <div className="border-b border-border px-3 py-2 text-sm font-medium">Active operations</div>
        <div className="max-h-80 overflow-y-auto">
          {active.isError && <p className="p-3 text-xs text-red-400">Could not load operations.</p>}
          {active.data?.items.length === 0 && (
            <p className="p-3 text-xs text-muted-foreground">Nothing is running right now.</p>
          )}
          {active.data?.items.map((op) => (
            <Link
              key={op.id}
              href={`/operations/${op.id}`}
              className="block space-y-1.5 border-b border-border/50 px-3 py-2 last:border-0 hover:bg-muted/40"
            >
              <div className="flex justify-between gap-2 text-sm">
                <span className="truncate">{op.title}</span>
                <span className="text-xs text-muted-foreground">{op.status}</span>
              </div>
              <Progress value={op.progress} className="h-1" />
            </Link>
          ))}
        </div>
        <div className="border-t border-border p-2">
          <Button asChild variant="ghost" size="sm" className="w-full">
            <Link href="/operations">View all operations</Link>
          </Button>
        </div>
      </PopoverContent>
    </Popover>
  );
}

function LiveIndicator() {
  const { mode } = useLiveUpdates();
  const label = { live: "Live updates connected", polling: "Live updates unavailable — polling", connecting: "Connecting…" }[mode];
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className="flex items-center gap-1.5 px-1 text-xs text-muted-foreground" aria-label={label}>
          <Radio className="size-3.5" />
          <StatusDot tone={mode === "live" ? "success" : mode === "polling" ? "warning" : "neutral"} pulse={mode === "live"} />
        </span>
      </TooltipTrigger>
      <TooltipContent>{label}</TooltipContent>
    </Tooltip>
  );
}

function UserMenu() {
  const info = useSystemInfo();
  const user = info.data?.current_user;
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="icon" aria-label="Account">
          <UserRound className="size-4" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-64">
        <DropdownMenuLabel className="space-y-0.5">
          <p className="text-sm">{user?.display_name ?? "…"}</p>
          <p className="font-mono text-[11px] font-normal text-muted-foreground">{user?.subject}</p>
        </DropdownMenuLabel>
        {info.data?.auth_mode === "dev" && (
          <p className="mx-2 mb-2 rounded border border-amber-500/30 bg-amber-500/10 p-2 text-[11px] text-amber-300">
            Development authentication: every request runs as a local administrator. OIDC sign-in arrives in Phase 7.
          </p>
        )}
        <DropdownMenuSeparator />
        <DropdownMenuItem asChild>
          <Link href="/administration/settings">Platform settings</Link>
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

export function Topbar() {
  const [menuOpen, setMenuOpen] = useState(false);
  return (
    <header className="sticky top-0 z-30 flex h-14 items-center gap-3 border-b border-border/60 bg-background/85 px-4 backdrop-blur">
      <Button variant="ghost" size="icon" className="lg:hidden" aria-label="Open navigation" onClick={() => setMenuOpen(true)}>
        <Menu className="size-5" />
      </Button>
      <Dialog open={menuOpen} onOpenChange={setMenuOpen}>
        <DialogContent className="top-0 left-0 h-full max-w-72 translate-x-0 translate-y-0 rounded-none bg-sidebar p-0 sm:max-w-72">
          <DialogTitle className="sr-only">Navigation</DialogTitle>
          <SidebarContent onNavigate={() => setMenuOpen(false)} />
        </DialogContent>
      </Dialog>
      <ClusterSelector />
      <div className="flex flex-1 justify-center">
        <GlobalSearch />
      </div>
      <div className="flex items-center gap-1">
        <LiveIndicator />
        <ActiveOperations />
        <UserMenu />
      </div>
    </header>
  );
}
