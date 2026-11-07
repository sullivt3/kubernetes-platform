import type { Metadata } from "next";

import { TopologyPage } from "@/features/kubernetes/scoped-pages";

export const metadata: Metadata = { title: "Topology" };

export default function Page() {
  return <TopologyPage />;
}
