"use client";

import { ChevronRight, Container } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { Suspense, useState } from "react";

import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { activeHref, NAVIGATION, type NavSection } from "@/lib/navigation";
import { cn } from "@/lib/utils";
import { useAuth } from "@/components/providers/auth";
import { pagePermission } from "@/lib/api/identity";

function SectionGroup({
  section,
  active,
  onNavigate,
}: {
  section: NavSection;
  active: string | undefined;
  onNavigate?: () => void;
}) {
  const containsActive = section.items.some((i) => i.href === active);
  const [open, setOpen] = useState(containsActive || section.title === "Overview");
  const Icon = section.icon;

  return (
    <Collapsible open={open || containsActive} onOpenChange={setOpen}>
      <CollapsibleTrigger className="group flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-xs font-medium tracking-wide text-muted-foreground uppercase hover:text-foreground">
        <Icon className="size-3.5" />
        <span className="flex-1 text-left">{section.title}</span>
        <ChevronRight className="size-3.5 transition-transform group-data-[state=open]:rotate-90" />
      </CollapsibleTrigger>
      <CollapsibleContent>
        <ul className="mt-0.5 mb-2 space-y-px border-l border-border/60 pl-2 ml-3.5">
          {section.items.map((item) => {
            const isActive = item.href === active;
            const planned = item.phase !== undefined;
            return (
              <li key={item.href}>
                <Link
                  href={item.href}
                  onClick={onNavigate}
                  aria-current={isActive ? "page" : undefined}
                  className={cn(
                    "flex items-center justify-between gap-2 rounded-md px-2 py-1.5 text-sm transition-colors",
                    isActive
                      ? "bg-sidebar-accent text-sidebar-accent-foreground font-medium"
                      : "text-sidebar-foreground/80 hover:bg-sidebar-accent/60 hover:text-sidebar-foreground",
                    planned && !isActive && "text-sidebar-foreground/45",
                  )}
                >
                  <span className="truncate">{item.title}</span>
                  {planned && (
                    <span
                      title={`Planned for Phase ${item.phase}`}
                      className="shrink-0 rounded px-1 font-mono text-[10px] text-muted-foreground/70"
                    >
                      P{item.phase}
                    </span>
                  )}
                </Link>
              </li>
            );
          })}
        </ul>
      </CollapsibleContent>
    </Collapsible>
  );
}

function NavTree({ onNavigate }: { onNavigate?: () => void }) {
  const active = activeHref(usePathname());
  const auth = useAuth();
  const visible = NAVIGATION.map((section) => ({ ...section, items: section.items.filter((item) => auth?.context?.permissions.includes(pagePermission(item.href))) })).filter((section) => section.items.length > 0);
  return (
    <>
      {visible.map((section) => (
        <SectionGroup key={section.title} section={section} active={active} onNavigate={onNavigate} />
      ))}
    </>
  );
}

function NavTreeFallback() {
  return (
    <>
      {NAVIGATION.map((section) => (
        <div key={section.title} className="flex items-center gap-2 px-2 py-1.5 text-xs uppercase text-muted-foreground">
          <section.icon className="size-3.5" />
          {section.title}
        </div>
      ))}
    </>
  );
}

export function SidebarContent({ onNavigate }: { onNavigate?: () => void }) {
  return (
    <div className="flex h-full flex-col">
      <Link href="/" onClick={onNavigate} className="flex h-14 shrink-0 items-center gap-2.5 border-b border-sidebar-border px-4">
        <div className="flex size-7 items-center justify-center rounded-md bg-sky-500/15 text-sky-400">
          <Container className="size-4" />
        </div>
        <div className="leading-tight">
          <p className="text-sm font-semibold">Kubernetes Platform</p>
          <p className="text-[11px] text-muted-foreground">Infrastructure management</p>
        </div>
      </Link>
      <nav aria-label="Main navigation" className="flex-1 space-y-0.5 overflow-y-auto px-2 py-3">
        {/* usePathname must sit under Suspense with cacheComponents enabled. */}
        <Suspense fallback={<NavTreeFallback />}>
          <NavTree onNavigate={onNavigate} />
        </Suspense>
      </nav>
      <div className="border-t border-sidebar-border px-4 py-3 text-[11px] text-muted-foreground">
        Items marked <span className="font-mono">P#</span> are planned for that roadmap phase.
      </div>
    </div>
  );
}

export function Sidebar() {
  return (
    <aside className="hidden w-64 shrink-0 border-r border-sidebar-border bg-sidebar lg:block">
      <div className="sticky top-0 h-screen">
        <SidebarContent />
      </div>
    </aside>
  );
}
