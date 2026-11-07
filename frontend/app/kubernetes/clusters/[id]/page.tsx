import type { Metadata } from "next";
import { Suspense } from "react";

import { Skeleton } from "@/components/ui/skeleton";
import { ClusterDetailView } from "@/features/clusters/cluster-detail";

export const metadata: Metadata = { title: "Cluster" };

export default function Page({ params }: PageProps<"/kubernetes/clusters/[id]">) {
  return (
    <Suspense fallback={<Skeleton className="h-96" />}>
      <ClusterDetailView params={params} />
    </Suspense>
  );
}
