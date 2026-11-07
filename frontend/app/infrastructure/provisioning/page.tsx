import type { Metadata } from "next";
import { Suspense } from "react";

import { Skeleton } from "@/components/ui/skeleton";
import { ProvisioningPage } from "@/features/infrastructure/provisioning";

export const metadata: Metadata = { title: "Provision machines" };

export default function Page() {
  return (
    <Suspense fallback={<Skeleton className="h-96" />}>
      <ProvisioningPage />
    </Suspense>
  );
}
