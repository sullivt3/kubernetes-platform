import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { PlannedFeature } from "@/components/common/planned-feature";
import { findNavItem, findNavSection, PLANNED_ITEMS } from "@/lib/navigation";

// Every navigation entry that is not implemented yet is served by this route, so
// the sidebar never links to a dead page and nothing pretends to work.
export function generateStaticParams() {
  return PLANNED_ITEMS.map((item) => ({ slug: item.href.split("/").filter(Boolean) }));
}

function resolve(slug: string[]) {
  const href = `/${slug.join("/")}`;
  const item = findNavItem(href);
  return item && item.phase !== undefined ? { item, section: findNavSection(href) } : null;
}

export async function generateMetadata({ params }: PageProps<"/[...slug]">): Promise<Metadata> {
  const resolved = resolve((await params).slug);
  return { title: resolved?.item.title ?? "Not found" };
}

export default async function PlannedFeaturePage({ params }: PageProps<"/[...slug]">) {
  const resolved = resolve((await params).slug);
  if (!resolved) notFound();
  return <PlannedFeature item={resolved.item} section={resolved.section} />;
}
