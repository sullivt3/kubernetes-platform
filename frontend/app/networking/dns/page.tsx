import type { Metadata } from "next";

import { DnsPage } from "@/features/networking/dns";

export const metadata: Metadata = { title: "DNS & service discovery" };

export default function Page() {
  return <DnsPage />;
}
