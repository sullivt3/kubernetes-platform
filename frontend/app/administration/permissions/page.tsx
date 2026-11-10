import type { Metadata } from "next";
import { PermissionsAdmin } from "@/features/administration/identity";
export const metadata: Metadata = { title: "Permissions" };
export default function PermissionsPage() { return <PermissionsAdmin />; }
