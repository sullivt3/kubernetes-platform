import type { Metadata } from "next";
import { AlertsPage } from "@/features/monitoring/alerts";

export const metadata: Metadata = { title: "Alerts" };
export default function Page() { return <AlertsPage />; }
