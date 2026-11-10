import type { Metadata } from "next";
import { OrganizationsAdmin } from "@/features/administration/identity";
export const metadata: Metadata = { title: "Organizations & projects" };
export default function OrganizationsPage() { return <OrganizationsAdmin />; }
