import type { Metadata } from "next";

import { EventsPage } from "@/features/kubernetes/scoped-pages";

export const metadata: Metadata = { title: "Events" };

export default function Page() {
  return <EventsPage />;
}
