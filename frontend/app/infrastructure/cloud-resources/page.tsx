import type { Metadata } from "next";

import { CloudResourcesPage } from "@/features/infrastructure/inventory";

export const metadata: Metadata = { title: "Cloud resources" };

export default function Page() {
  return <CloudResourcesPage />;
}
