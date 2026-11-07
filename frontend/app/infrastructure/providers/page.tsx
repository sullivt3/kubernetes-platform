import type { Metadata } from "next";

import { ProviderCatalog } from "@/features/infrastructure/catalog";

export const metadata: Metadata = { title: "Infrastructure providers" };

export default function ProvidersPage() {
  return <ProviderCatalog />;
}
