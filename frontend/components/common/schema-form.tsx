"use client";

// Renders a form from the JSON schema the backend publishes for each provider's
// configuration (pydantic `model_json_schema`). The backend validates the result again.

import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import type { JsonSchema } from "@/lib/api/types";
import { humanize } from "@/lib/format";

export type SchemaValues = Record<string, string | boolean>;

interface FieldInfo {
  name: string;
  type: "string" | "integer" | "number" | "boolean" | "array";
  options: string[] | null;
  required: boolean;
  description?: string;
  title: string;
  defaultValue: unknown;
  multiline: boolean;
}

type Prop = JsonSchema & { default?: unknown; enum?: unknown[]; anyOf?: Prop[]; items?: Prop };

export function schemaFields(schema: JsonSchema): FieldInfo[] {
  const required = new Set(schema.required ?? []);
  return Object.entries((schema.properties ?? {}) as Record<string, Prop>).map(([name, prop]) => {
    const variant = prop.anyOf?.find((v) => v.type !== "null") ?? prop;
    const options = (variant.enum ?? prop.enum)?.map(String) ?? null;
    const rawType = String(variant.type ?? "string");
    const type = (["integer", "number", "boolean", "array"].includes(rawType) ? rawType : "string") as FieldInfo["type"];
    return {
      name,
      type,
      options,
      required: required.has(name),
      description: prop.description ?? variant.description,
      title: humanize(name),
      defaultValue: prop.default,
      multiline: /certificate|pem/i.test(name),
    };
  });
}

/** Initial form values: defaults rendered as text, booleans as booleans. */
export function schemaDefaults(schema: JsonSchema): SchemaValues {
  const values: SchemaValues = {};
  for (const field of schemaFields(schema)) {
    if (field.type === "boolean") values[field.name] = field.defaultValue === true;
    else if (Array.isArray(field.defaultValue)) values[field.name] = field.defaultValue.join(", ");
    else values[field.name] = field.defaultValue == null ? "" : String(field.defaultValue);
  }
  return values;
}

/** Convert form values to the JSON the API expects; empty optional fields are omitted. */
export function schemaPayload(schema: JsonSchema, values: SchemaValues): Record<string, unknown> {
  const payload: Record<string, unknown> = {};
  for (const field of schemaFields(schema)) {
    const value = values[field.name];
    if (field.type === "boolean") {
      payload[field.name] = Boolean(value);
      continue;
    }
    const text = String(value ?? "").trim();
    if (!text) continue;
    if (field.type === "integer" || field.type === "number") payload[field.name] = Number(text);
    else if (field.type === "array") payload[field.name] = text.split(",").map((v) => v.trim()).filter(Boolean);
    else payload[field.name] = text;
  }
  return payload;
}

export function missingRequired(schema: JsonSchema, values: SchemaValues): string[] {
  return schemaFields(schema)
    .filter((f) => f.required && f.type !== "boolean" && !String(values[f.name] ?? "").trim())
    .map((f) => f.title);
}

export function SchemaForm({
  schema,
  values,
  onChange,
  idPrefix = "cfg",
}: {
  schema: JsonSchema;
  values: SchemaValues;
  onChange: (values: SchemaValues) => void;
  idPrefix?: string;
}) {
  const fields = schemaFields(schema);
  if (!fields.length) return <p className="text-sm text-muted-foreground">No configuration required.</p>;
  const set = (name: string, value: string | boolean) => onChange({ ...values, [name]: value });
  return (
    <div className="grid gap-4 sm:grid-cols-2">
      {fields.map((field) => {
        const id = `${idPrefix}-${field.name}`;
        const wide = field.multiline || (field.description?.length ?? 0) > 70;
        if (field.type === "boolean") {
          return (
            <label key={field.name} className="flex items-start gap-2 text-sm sm:col-span-2">
              <input
                type="checkbox"
                className="mt-1"
                checked={Boolean(values[field.name])}
                onChange={(e) => set(field.name, e.target.checked)}
              />
              <span>
                {field.title}
                {field.description && <span className="block text-xs text-muted-foreground">{field.description}</span>}
              </span>
            </label>
          );
        }
        return (
          <div key={field.name} className={wide ? "space-y-1.5 sm:col-span-2" : "space-y-1.5"}>
            <Label htmlFor={id}>
              {field.title}
              {field.required ? <span className="text-red-400">*</span> : <span className="text-xs font-normal text-muted-foreground">(optional)</span>}
            </Label>
            {field.options ? (
              <Select value={String(values[field.name] ?? "")} onValueChange={(v) => set(field.name, v)}>
                <SelectTrigger id={id} className="w-full"><SelectValue placeholder="Choose…" /></SelectTrigger>
                <SelectContent>
                  {field.options.map((o) => <SelectItem key={o} value={o}>{humanize(o)}</SelectItem>)}
                </SelectContent>
              </Select>
            ) : field.multiline ? (
              <Textarea id={id} rows={4} className="font-mono text-xs" value={String(values[field.name] ?? "")} onChange={(e) => set(field.name, e.target.value)} />
            ) : (
              <Input
                id={id}
                value={String(values[field.name] ?? "")}
                onChange={(e) => set(field.name, e.target.value)}
                inputMode={field.type === "integer" || field.type === "number" ? "numeric" : undefined}
                placeholder={field.type === "array" ? "comma-separated" : undefined}
                className={field.type === "string" ? "font-mono text-xs" : undefined}
              />
            )}
            {field.description && <p className="text-xs text-muted-foreground">{field.description}</p>}
          </div>
        );
      })}
    </div>
  );
}
