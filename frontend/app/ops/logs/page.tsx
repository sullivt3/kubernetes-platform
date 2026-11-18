import type { Metadata } from "next";

import { LogsPage } from "@/features/logs/logs";

export const metadata: Metadata = { title: "Logs" };

export default function Page() {
  return <LogsPage />;
}
