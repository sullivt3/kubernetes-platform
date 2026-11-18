import type { Metadata } from "next";
import { BackupsPage } from "@/features/backups/backups";

export const metadata: Metadata = { title: "Backup & restore" };
export default function Page() {
  return <BackupsPage />;
}
