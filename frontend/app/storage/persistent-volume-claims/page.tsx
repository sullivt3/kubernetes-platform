import type { Metadata } from "next";

import { ResourcePage } from "@/features/workloads/resource-list";

export const metadata: Metadata = { title: "Persistent volume claims" };

export default function Page() {
  return <ResourcePage kind="persistentvolumeclaims" />;
}
