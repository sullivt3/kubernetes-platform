import type { Metadata } from "next";

import { ResourcePage } from "@/features/workloads/resource-list";

export const metadata: Metadata = { title: "Service accounts" };

export default function Page() {
  return <ResourcePage kind="serviceaccounts" />;
}
