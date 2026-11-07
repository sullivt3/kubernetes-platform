import { Construction } from "lucide-react";
import Link from "next/link";

import { PageHeader } from "@/components/common/page";
import { StatusPill } from "@/components/common/status";
import { PHASES, type NavItem, type NavSection } from "@/lib/navigation";

/**
 * Shown for navigation entries whose functionality is not implemented yet. It never
 * renders sample data or interactive controls that would imply the feature works.
 */
export function PlannedFeature({ item, section }: { item: NavItem; section?: NavSection }) {
  const phase = item.phase ?? 0;
  const available = section?.items.filter((i) => i.phase === undefined && i.href !== item.href) ?? [];
  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow={section?.title}
        title={item.title}
        description={item.description}
        actions={<StatusPill tone="neutral">Not yet available</StatusPill>}
      />
      <div className="flex gap-4 rounded-lg border border-border/70 bg-card/40 p-5">
        <Construction className="mt-0.5 size-5 shrink-0 text-amber-400" />
        <div className="space-y-2 text-sm">
          <p className="font-medium">
            Planned for Phase {phase}: {PHASES[phase]}
          </p>
          <p className="text-muted-foreground">
            This capability has not been implemented yet. The platform does not show sample or simulated data
            here: once implemented, this page will display live information from your infrastructure and
            clusters.
          </p>
          {available.length > 0 && (
            <p className="text-muted-foreground">
              Available now in {section?.title}:{" "}
              {available.map((i, index) => (
                <span key={i.href}>
                  {index > 0 && ", "}
                  <Link className="text-foreground underline underline-offset-4" href={i.href}>
                    {i.title}
                  </Link>
                </span>
              ))}
            </p>
          )}
        </div>
      </div>
    </div>
  );
}
