import type { Metadata } from "next";

import { CsiDriversPage } from "@/features/storage/csi-drivers";

export const metadata: Metadata = { title: "CSI drivers" };

export default function Page() {
  return <CsiDriversPage />;
}
