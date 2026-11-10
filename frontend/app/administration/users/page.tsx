import type { Metadata } from "next";
import { UsersAdmin } from "@/features/administration/identity";
export const metadata: Metadata = { title: "Users" };
export default function UsersPage() { return <UsersAdmin />; }
