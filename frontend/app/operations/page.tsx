import type { Metadata } from "next";

import { OperationsList } from "@/features/operations/operations-list";

export const metadata: Metadata = { title: "Operations" };

export default function OperationsPage() {
  return <OperationsList />;
}
