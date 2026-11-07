import type { Metadata } from "next";

import { ClusterSettingsPage } from "@/features/kubernetes/scoped-pages";

export const metadata: Metadata = { title: "Cluster settings" };

export default function Page() {
  return <ClusterSettingsPage />;
}
