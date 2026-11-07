import type { Metadata } from "next";

import { CreateClusterWizard } from "@/features/clusters/create-wizard";

export const metadata: Metadata = { title: "Create cluster" };

export default function Page() {
  return <CreateClusterWizard />;
}
