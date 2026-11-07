import type { Metadata } from "next";

import { NetworkDiscoveryPage } from "@/features/infrastructure/discovery";

export const metadata: Metadata = { title: "Network discovery" };

export default function Page() {
  return <NetworkDiscoveryPage />;
}
