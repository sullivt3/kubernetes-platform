import type { Metadata } from "next";

import { AuditLog } from "@/features/administration/audit-log";

export const metadata: Metadata = { title: "Audit log" };

export default function AuditLogPage() {
  return <AuditLog />;
}
