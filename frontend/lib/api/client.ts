import type { ApiErrorBody } from "./types";

export const API_BASE = "/api/v1";

/** A structured error returned by the backend (or a transport failure). */
export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
    message: string,
    public readonly details: Record<string, unknown> = {},
    public readonly requestId: string | null = null,
  ) {
    super(message);
    this.name = "ApiError";
  }

  get isUnavailable(): boolean {
    return this.status === 0 || this.status === 502 || this.status === 503 || this.status === 504;
  }
}

function isErrorBody(value: unknown): value is ApiErrorBody {
  return (
    typeof value === "object" &&
    value !== null &&
    "error" in value &&
    typeof (value as ApiErrorBody).error?.message === "string"
  );
}

export type QueryValue = string | number | boolean | null | undefined | Array<string | number>;

export function buildUrl(path: string, query?: Record<string, QueryValue>): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value === undefined || value === null || value === "") continue;
    if (Array.isArray(value)) value.forEach((v) => params.append(key, String(v)));
    else params.set(key, String(value));
  }
  const qs = params.toString();
  return `${API_BASE}${path}${qs ? `?${qs}` : ""}`;
}

interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  query?: Record<string, QueryValue>;
  body?: unknown;
  headers?: Record<string, string>;
  signal?: AbortSignal;
  /** "text" returns the raw body (plain-text endpoints such as logs). */
  responseType?: "json" | "text";
}

export async function apiRequest<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = "GET", query, body, headers, signal, responseType = "json" } = options;
  let response: Response;
  try {
    response = await fetch(buildUrl(path, query), {
      method,
      signal,
      credentials: "same-origin",
      headers: {
        Accept: responseType === "text" ? "text/plain, application/json" : "application/json",
        ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
        ...headers,
      },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch (error) {
    if ((error as Error).name === "AbortError") throw error;
    throw new ApiError(0, "network_error", "The platform API could not be reached.");
  }

  if (response.status === 204) return undefined as T;

  const text = await response.text();
  let payload: unknown = undefined;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = undefined;
    }
  }

  if (!response.ok) {
    if (isErrorBody(payload)) {
      const { code, message, details, request_id } = payload.error;
      throw new ApiError(response.status, code, message, details, request_id);
    }
    const message =
      response.status >= 500
        ? "The platform API is unavailable or returned an unexpected error."
        : `Request failed with status ${response.status}.`;
    throw new ApiError(response.status, "http_error", message, {}, response.headers.get("x-request-id"));
  }
  return (responseType === "text" ? text : payload) as T;
}

export const api = {
  get: <T>(path: string, query?: Record<string, QueryValue>) => apiRequest<T>(path, { query }),
  post: <T>(path: string, body?: unknown, headers?: Record<string, string>) =>
    apiRequest<T>(path, { method: "POST", body, headers }),
  put: <T>(path: string, body?: unknown) => apiRequest<T>(path, { method: "PUT", body }),
  delete: <T>(path: string, query?: Record<string, QueryValue>) =>
    apiRequest<T>(path, { method: "DELETE", query }),
};
