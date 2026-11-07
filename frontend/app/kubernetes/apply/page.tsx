import type { Metadata } from "next";

import { ApplyYamlPage } from "@/features/workloads/apply-page";

export const metadata: Metadata = { title: "Apply YAML" };

export default function Page() {
  return <ApplyYamlPage />;
}
