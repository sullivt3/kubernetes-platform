import type { Metadata } from "next";

import { ImportClusterPage } from "@/features/kubernetes/import-cluster";

export const metadata: Metadata = { title: "Import cluster" };

export default function Page() {
  return <ImportClusterPage />;
}
