"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { usePathname } from "next/navigation";
import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { toast } from "sonner";

import { ErrorState } from "@/components/common/page";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api/client";
import { getAuthSession, pagePermission, type AuthSession } from "@/lib/api/identity";

const AuthContext = createContext<AuthSession | null>(null);
export function useAuth() { return useContext(AuthContext); }
export function useCan(permission: string) { return useAuth()?.context?.permissions.includes(permission) ?? false; }

function reloadAtRoot() {
  // Drop the entire query/router cache and close streams after identity/context changes.
  // eslint-disable-next-line @next/next/no-location-assign-relative-destination
  window.location.assign("/");
}

export async function signOut() {
  try { await api.post("/auth/logout"); reloadAtRoot(); }
  catch (error) { toast.error("Could not sign out", { description: (error as Error).message }); }
}

export function ContextSelector({ session }: { session: AuthSession }) {
  const [orgId, setOrgId] = useState(session.context?.organization_id ?? session.organizations?.[0]?.id ?? "");
  const [projectId, setProjectId] = useState(session.context?.project_id ?? "");
  const [pending, setPending] = useState(false);
  const organization = session.organizations?.find((org) => org.id === orgId);
  return <form className="flex flex-wrap items-center gap-2" onSubmit={async (event) => {
    event.preventDefault(); setPending(true);
    try { await api.post("/auth/context", { organization_id: orgId, project_id: projectId || null }); reloadAtRoot(); }
    catch (error) { toast.error("Could not select context", { description: (error as Error).message }); setPending(false); }
  }}>
    <select aria-label="Organization" className="h-8 max-w-48 rounded border border-input bg-background px-2 text-xs" value={orgId} onChange={(event) => { setOrgId(event.target.value); setProjectId(""); }}>
      {session.organizations?.map((org) => <option key={org.id} value={org.id}>{org.name}</option>)}
    </select>
    <select aria-label="Project" className="h-8 max-w-44 rounded border border-input bg-background px-2 text-xs" value={projectId} onChange={(event) => setProjectId(event.target.value)}>
      <option value="" disabled={organization?.role === "member"}>{organization?.role === "member" ? "Select project" : "Entire organization"}</option>
      {organization?.projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}
    </select>
    <Button size="sm" variant="outline" disabled={pending || !orgId || (organization?.role === "member" && !projectId)}>Switch</Button>
  </form>;
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const session = useQuery({ queryKey: ["auth-session"], queryFn: getAuthSession, staleTime: 30_000, refetchInterval: 60_000 });
  const client = useQueryClient();
  useEffect(() => {
    const expired = () => { void client.invalidateQueries({ queryKey: ["auth-session"] }); };
    window.addEventListener("platform-auth-expired", expired);
    return () => window.removeEventListener("platform-auth-expired", expired);
  }, [client]);
  const data = session.data;
  let content: ReactNode;
  if (session.isError) content = <ErrorState error={session.error} onRetry={() => session.refetch()} />;
  else if (!data) content = <p role="status">Loading account…</p>;
  else if (!data.authenticated) content = <div className="space-y-4"><h1 className="text-xl font-semibold">Sign in to Kubernetes Platform</h1><p className="text-sm text-muted-foreground">Use your organization’s identity provider to continue.</p><Button asChild><a href={data.login_url}>Sign in</a></Button></div>;
  else if (!data.context || !data.context.permissions.includes("clusters.read")) content = <div className="space-y-4"><h1 className="text-xl font-semibold">Welcome, {data.user?.display_name}</h1><p className="text-sm text-muted-foreground">{data.context_error ?? "Select a project you belong to. An administrator can grant access using your identity below."}</p><p className="break-all font-mono text-xs">{data.user?.subject}</p>{!!data.organizations?.length && <ContextSelector session={data} />}<Button variant="outline" onClick={() => void signOut()}>Sign out</Button></div>;
  else return <AuthContext.Provider value={data}>{children}</AuthContext.Provider>;
  return <div className="mx-auto flex min-h-screen max-w-2xl items-center px-6"><div className="w-full rounded-xl border bg-card p-8">{content}</div></div>;
}

/** Route visibility supplements the backend's independently enforced policy. */
export function AuthorizedPage({ children }: { children: ReactNode }) {
  const path = usePathname();
  const allowed = useCan(pagePermission(path));
  const write = useCan("clusters.write");
  if (!allowed) return <div role="alert" className="rounded border p-6"><h1 className="text-lg font-semibold">Access restricted</h1><p className="mt-2 text-sm text-muted-foreground">Your current role does not allow this page. Select another context or contact an administrator.</p></div>;
  return <>{!write && <p className="mb-5 rounded border border-sky-500/30 bg-sky-500/10 p-3 text-sm">Viewer access: you can inspect resources. Changes, secret reveals, and terminal access require an operator or administrator role.</p>}{children}</>;
}
