import type { Metadata } from "next";

import { MachinesPage } from "@/features/infrastructure/machines";

export const metadata: Metadata = { title: "Machines" };

export default function Page() {
  return <MachinesPage />;
}
