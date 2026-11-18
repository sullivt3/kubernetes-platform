import type { Metadata } from "next";

import { SecurityPoliciesPage } from "@/features/security/policies";

export const metadata: Metadata = { title: "Security policies" };

export default function Page() {
  return <SecurityPoliciesPage />;
}
