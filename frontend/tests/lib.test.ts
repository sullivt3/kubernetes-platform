import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, apiRequest, buildUrl } from "@/lib/api/client";
import { formatDuration, formatRelative, humanize } from "@/lib/format";

describe("format", () => {
  const now = Date.parse("2026-01-01T12:00:00Z");

  it("formats relative times", () => {
    expect(formatRelative("2026-01-01T11:59:58Z", now)).toBe("just now");
    expect(formatRelative("2026-01-01T11:55:00Z", now)).toBe("5 minutes ago");
    expect(formatRelative(null, now)).toBe("—");
  });

  it("formats durations", () => {
    expect(formatDuration("2026-01-01T11:59:30Z", "2026-01-01T12:00:00Z")).toBe("30s");
    expect(formatDuration("2026-01-01T11:57:55Z", "2026-01-01T12:00:00Z")).toBe("2m 5s");
    expect(formatDuration("2026-01-01T09:30:00Z", "2026-01-01T12:00:00Z")).toBe("2h 30m");
    expect(formatDuration(null, null)).toBe("—");
  });

  it("humanizes identifiers", () => {
    expect(humanize("ssh_private_key")).toBe("SSH private key");
    expect(humanize("aws_access_key")).toBe("AWS access key");
    expect(humanize("validate_connection")).toBe("Validate connection");
  });
});

describe("api client", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("builds query strings, skipping empty values and repeating arrays", () => {
    expect(buildUrl("/operations", { status: ["failed", "cancelled"], active: undefined, limit: 10, type: "" })).toBe(
      "/api/v1/operations?status=failed&status=cancelled&limit=10",
    );
  });

  it("maps structured backend errors to ApiError", async () => {
    const body = { error: { code: "conflict", message: "Busy", details: { key: "x" }, request_id: "r1" } };
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(body), { status: 409 })));
    const error = await apiRequest("/x").catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 409, code: "conflict", message: "Busy", requestId: "r1" });
  });

  it("reports an unreachable backend as unavailable", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Promise.reject(new TypeError("fetch failed"))));
    const error = (await apiRequest("/x").catch((e: unknown) => e)) as ApiError;
    expect(error.code).toBe("network_error");
    expect(error.isUnavailable).toBe(true);
  });

  it("handles proxy errors without a JSON body", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("Bad Gateway", { status: 502 })));
    const error = (await apiRequest("/x").catch((e: unknown) => e)) as ApiError;
    expect(error.isUnavailable).toBe(true);
    expect(error.message).toMatch(/unavailable/);
  });

  it("returns undefined for 204 responses", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(null, { status: 204 })));
    await expect(apiRequest("/x", { method: "DELETE" })).resolves.toBeUndefined();
  });
});
