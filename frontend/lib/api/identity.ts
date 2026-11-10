import { api } from "./client";

export interface TenantContext {
  id: string;
  name: string;
  slug: string;
  role: string;
  projects: { id: string; name: string; slug: string }[];
}
export interface AuthSession {
  authenticated: boolean;
  auth_mode: "dev" | "oidc";
  login_url: string;
  user?: { id: string; subject: string; display_name: string; is_platform_admin: boolean };
  organizations?: TenantContext[];
  context?: { organization_id: string; project_id: string | null; roles: string[]; permissions: string[] } | null;
  context_error?: string | null;
}
export interface PlatformUser {
  id: string;
  subject: string;
  display_name: string;
  email: string | null;
  is_active: boolean;
  is_platform_admin: boolean;
  last_seen_at: string | null;
}
export interface Memberships {
  organization: { user: PlatformUser; role: string }[];
  projects: { user: PlatformUser; role: string; project_id: string; project_name: string }[];
}

export const getAuthSession = () => api.get<AuthSession>("/auth/session");

export function pagePermission(path: string): string {
  if (path === "/administration/users") return "platform.manage";
  if (path === "/administration/organizations") return "organization.manage";
  if (path === "/administration/audit-log") return "audit.read";
  if (path.startsWith("/administration")) return "system.read";
  if (["/kubernetes/create", "/kubernetes/import", "/kubernetes/apply", "/kubernetes/upgrades"].includes(path)) return "clusters.write";
  if (["/infrastructure/provisioning", "/infrastructure/discovery"].includes(path)) return "infrastructure.write";
  if (path.startsWith("/infrastructure")) return "infrastructure.read";
  if (path.startsWith("/security/credentials")) return "credentials.read";
  if (path.startsWith("/operations")) return "operations.read";
  if (path.startsWith("/kubernetes") || path.startsWith("/workloads") || path.startsWith("/configuration") || path.startsWith("/networking") || path.startsWith("/storage")) return "clusters.read";
  return "system.read";
}
