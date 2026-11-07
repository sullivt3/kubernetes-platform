import type { Metadata } from "next";

import { ResourcePage } from "@/features/workloads/resource-list";

export const metadata: Metadata = { title: "CronJobs" };

export default function Page() {
  return <ResourcePage kind="cronjobs" />;
}
