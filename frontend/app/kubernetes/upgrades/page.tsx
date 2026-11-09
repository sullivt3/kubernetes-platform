import type { Metadata } from "next";

import { ClusterUpgrades } from "@/features/clusters/upgrades";

export const metadata: Metadata = { title: "Cluster upgrades" };

export default function Page() {
  return <ClusterUpgrades />;
}
