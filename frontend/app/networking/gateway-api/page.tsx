import type { Metadata } from "next";

import { GatewayApiPage } from "@/features/networking/gateway-api";

export const metadata: Metadata = { title: "Gateway API" };

export default function Page() {
  return <GatewayApiPage />;
}
