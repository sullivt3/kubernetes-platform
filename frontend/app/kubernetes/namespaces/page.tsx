import type { Metadata } from "next";

import { NamespacesPage } from "@/features/kubernetes/scoped-pages";

export const metadata: Metadata = { title: "Namespaces" };

export default function Page() {
  return <NamespacesPage />;
}
