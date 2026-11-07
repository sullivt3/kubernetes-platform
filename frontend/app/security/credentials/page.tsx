import type { Metadata } from "next";

import { CredentialsPage } from "@/features/security/credentials";

export const metadata: Metadata = { title: "Credentials" };

export default function Page() {
  return <CredentialsPage />;
}
