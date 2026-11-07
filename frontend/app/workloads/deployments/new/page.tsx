import type { Metadata } from "next";

import { NewDeploymentPage } from "@/features/workloads/deployment-form";

export const metadata: Metadata = { title: "New deployment" };

export default function Page() {
  return <NewDeploymentPage />;
}
