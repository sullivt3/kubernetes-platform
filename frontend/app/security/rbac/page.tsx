import type { Metadata } from "next";

import { RbacPage } from "@/features/security/rbac";

export const metadata: Metadata = { title: "Roles & bindings" };

export default function Page() {
  return <RbacPage />;
}
