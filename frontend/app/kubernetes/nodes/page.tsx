import type { Metadata } from "next";

import { NodesPage } from "@/features/kubernetes/scoped-pages";

export const metadata: Metadata = { title: "Nodes" };

export default function Page() {
  return <NodesPage />;
}
