import type { Metadata } from "next";

import { DistributionCatalog } from "@/features/infrastructure/catalog";

export const metadata: Metadata = { title: "Kubernetes distributions" };

export default function DistributionsPage() {
  return <DistributionCatalog />;
}
