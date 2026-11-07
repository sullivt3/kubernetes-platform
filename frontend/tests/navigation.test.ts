import { existsSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

import { activeHref, ALL_NAV_ITEMS, NAVIGATION, PHASES, PLANNED_ITEMS } from "@/lib/navigation";

const APP_DIR = path.resolve(__dirname, "../app");

describe("navigation", () => {
  it("has unique hrefs", () => {
    const hrefs = ALL_NAV_ITEMS.map((i) => i.href);
    expect(new Set(hrefs).size).toBe(hrefs.length);
  });

  it("covers every top-level section from the product specification", () => {
    expect(NAVIGATION.map((s) => s.title)).toEqual([
      "Overview",
      "Infrastructure",
      "Kubernetes",
      "Workloads",
      "Networking",
      "Storage",
      "Configuration",
      "Security",
      "Operations",
      "Administration",
    ]);
  });

  it("labels planned items with a known roadmap phase", () => {
    for (const item of PLANNED_ITEMS) {
      expect(PHASES[item.phase!], item.href).toBeDefined();
    }
  });

  it("backs every implemented item with a real page (no dead links)", () => {
    for (const item of ALL_NAV_ITEMS.filter((i) => i.phase === undefined)) {
      const page = path.join(APP_DIR, ...item.href.split("/").filter(Boolean), "page.tsx");
      expect(existsSync(page), `${item.href} -> ${page}`).toBe(true);
    }
  });

  it("does not let a planned item collide with an implemented route", () => {
    for (const item of PLANNED_ITEMS) {
      const page = path.join(APP_DIR, ...item.href.split("/").filter(Boolean), "page.tsx");
      expect(existsSync(page), `${item.href} is planned but has its own page`).toBe(false);
      expect(item.href.startsWith("/operations/"), `${item.href} would be captured by /operations/[id]`).toBe(false);
    }
  });

  it("resolves the active item for nested routes", () => {
    expect(activeHref("/")).toBe("/");
    expect(activeHref("/operations")).toBe("/operations");
    expect(activeHref("/operations/0b6f6a0e-1111-2222-3333-444444444444")).toBe("/operations");
    expect(activeHref("/does/not/exist")).toBeUndefined();
  });
});
