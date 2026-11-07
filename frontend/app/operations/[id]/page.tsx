import type { Metadata } from "next";
import { Suspense } from "react";

import { Skeleton } from "@/components/ui/skeleton";
import { OperationDetailView } from "@/features/operations/operation-detail";

export const metadata: Metadata = { title: "Operation" };

export default function OperationPage({ params }: PageProps<"/operations/[id]">) {
  // params is request-time data: resolve it inside a Suspense boundary.
  return (
    <Suspense fallback={<Skeleton className="h-96" />}>
      <OperationDetailView params={params} />
    </Suspense>
  );
}
