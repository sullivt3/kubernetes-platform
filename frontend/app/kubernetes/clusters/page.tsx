import type { Metadata } from "next";

import { ClustersList } from "@/features/clusters/clusters-list";

export const metadata: Metadata = { title: "Clusters" };

export default function Page() {
  return <ClustersList />;
}
