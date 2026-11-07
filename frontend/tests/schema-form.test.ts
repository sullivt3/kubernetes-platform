import { describe, expect, it } from "vitest";

import { missingRequired, schemaDefaults, schemaFields, schemaPayload } from "@/components/common/schema-form";
import { expandNames } from "@/lib/api/infra";
import type { JsonSchema } from "@/lib/api/types";

// Shape produced by pydantic's model_json_schema() for a provider config.
const schema: JsonSchema = {
  type: "object",
  required: ["api_url", "template_vmid"],
  properties: {
    api_url: { type: "string", description: "e.g. https://pve:8006" },
    template_vmid: { type: "integer" },
    default_storage: { anyOf: [{ type: "string" }, { type: "null" }], default: null },
    access: { enum: ["published_ports", "container_network"], type: "string", default: "published_ports" },
    enforce_resources: { type: "boolean", default: true },
    discovery_regions: { type: "array", items: { type: "string" } },
    ca_certificate: { anyOf: [{ type: "string" }, { type: "null" }], default: null },
  },
};

describe("schema form", () => {
  it("derives field types, options and required flags", () => {
    const fields = Object.fromEntries(schemaFields(schema).map((f) => [f.name, f]));
    expect(fields.template_vmid.type).toBe("integer");
    expect(fields.default_storage.type).toBe("string");
    expect(fields.access.options).toEqual(["published_ports", "container_network"]);
    expect(fields.api_url.required).toBe(true);
    expect(fields.ca_certificate.multiline).toBe(true);
  });

  it("builds a payload without empty optional fields", () => {
    const values = { ...schemaDefaults(schema), api_url: " https://pve:8006 ", template_vmid: "9000", discovery_regions: "us-east-1, eu-west-1" };
    expect(missingRequired(schema, values)).toEqual([]);
    expect(schemaPayload(schema, values)).toEqual({
      api_url: "https://pve:8006",
      template_vmid: 9000,
      access: "published_ports",
      enforce_resources: true,
      discovery_regions: ["us-east-1", "eu-west-1"],
    });
  });

  it("reports missing required fields", () => {
    expect(missingRequired(schema, schemaDefaults(schema))).toEqual(["API url", "Template vmid"]);
  });
});

describe("machine names", () => {
  it("expands a prefix and count", () => {
    expect(expandNames("node", 3, 2)).toEqual(["node-2", "node-3", "node-4"]);
  });
});
