import type { Metadata } from "next";

import { VirtualMachinesPage } from "@/features/infrastructure/inventory";

export const metadata: Metadata = { title: "Virtual machines" };

export default function Page() {
  return <VirtualMachinesPage />;
}
