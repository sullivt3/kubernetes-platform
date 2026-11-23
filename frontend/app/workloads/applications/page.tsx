import type { Metadata } from "next";
import { HelmPage } from "@/features/helm/releases";

export const metadata: Metadata = { title: "Helm releases" };
export default function Page() { return <HelmPage />; }
