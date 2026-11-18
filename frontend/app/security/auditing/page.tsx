import type { Metadata } from "next";

import { SecurityReportingPage } from "@/features/security/report";

export const metadata: Metadata = { title: "Security reporting" };

export default function Page() {
  return <SecurityReportingPage />;
}
