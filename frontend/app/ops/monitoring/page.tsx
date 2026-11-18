import type { Metadata } from "next";

import { MonitoringPage } from "@/features/monitoring/monitoring";

export const metadata: Metadata = { title: "Monitoring" };

export default function Page() {
  return <MonitoringPage />;
}
