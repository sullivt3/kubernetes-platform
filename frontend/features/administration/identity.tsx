"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { toast } from "sonner";

import { ConfirmDialog } from "@/components/common/confirm-dialog";
import { ErrorState, PageHeader } from "@/components/common/page";
import { ContextSelector, useAuth, useCan } from "@/components/providers/auth";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { api, apiRequest } from "@/lib/api/client";
import type { Memberships, PlatformUser } from "@/lib/api/identity";

interface Command { path: string; method: "POST" | "PUT" | "DELETE"; body?: unknown; query?: Record<string, string> }
interface Confirmation { title: string; subject: string; command: Command }
function useAdminAction(onSuccess?: () => void) {
  const client = useQueryClient();
  return useMutation({ mutationFn: (command: Command) => apiRequest(command.path, command), onSuccess: () => {
    void client.invalidateQueries({ queryKey: ["admin"] });
    void client.invalidateQueries({ queryKey: ["auth-session"] });
    toast.success("Saved"); onSuccess?.();
  }, onError: (error) => toast.error("Could not save", { description: error.message }) });
}
function ActionConfirmation({ confirmation, onClose }: { confirmation: Confirmation | null; onClose: () => void }) {
  const action = useAdminAction(onClose);
  return <ConfirmDialog open={!!confirmation} onOpenChange={(open) => !open && onClose()} title={confirmation?.title ?? "Confirm change"} description="This changes access to the platform. Active requests use the updated permissions on their next authorization check." confirmText={confirmation?.subject} confirmLabel="Confirm" pending={action.isPending} onConfirm={() => confirmation && action.mutate(confirmation.command)} />;
}

export function UsersAdmin() {
  const [page, setPage] = useState(0);
  const [confirmation, setConfirmation] = useState<Confirmation | null>(null);
  const users = useQuery({ queryKey: ["admin", "users", page], queryFn: () => api.get<PlatformUser[]>("/administration/users", { limit: 100, offset: page * 100 }) });
  function change(user: PlatformUser, admin: boolean, active: boolean) { setConfirmation({ title: `Update ${user.display_name}`, subject: user.subject, command: { path: `/administration/users/${user.id}`, method: "PUT", body: { is_platform_admin: admin, is_active: active } } }); }
  return <div className="space-y-6">
    <PageHeader eyebrow="Administration" title="Users" description="Users appear after their first OIDC sign-in. Disabling an account revokes its browser sessions and blocks bearer-token access. Tenant permissions are assigned under Organizations & projects." />
    {users.isLoading && <p role="status">Loading users…</p>}
    {users.isError && <ErrorState error={users.error} onRetry={() => users.refetch()} />}
    <Card><CardContent><Table><TableHeader><TableRow><TableHead>User</TableHead><TableHead>Access</TableHead><TableHead>Actions</TableHead></TableRow></TableHeader><TableBody>
      {users.data?.map((user) => <TableRow key={user.id}><TableCell><p>{user.display_name}</p><p className="max-w-xl break-all font-mono text-xs text-muted-foreground">{user.subject}</p><p className="text-xs text-muted-foreground">{user.email}</p></TableCell><TableCell>{user.is_active ? "Active" : "Disabled"}{user.is_platform_admin ? " · Platform administrator" : ""}</TableCell><TableCell><div className="flex flex-wrap gap-2"><Button size="sm" variant="outline" disabled={user.subject.startsWith("dev|")} onClick={() => change(user, user.is_platform_admin, !user.is_active)}>{user.is_active ? "Disable" : "Enable"}</Button><Button size="sm" variant="outline" disabled={user.subject.startsWith("dev|")} onClick={() => change(user, !user.is_platform_admin, user.is_active)}>{user.is_platform_admin ? "Remove platform admin" : "Grant platform admin"}</Button></div></TableCell></TableRow>)}
    </TableBody></Table></CardContent></Card>
    <div className="flex justify-end gap-2"><Button variant="outline" disabled={!page} onClick={() => setPage(page - 1)}>Previous</Button><Button variant="outline" disabled={(users.data?.length ?? 0) < 100} onClick={() => setPage(page + 1)}>Next</Button></div>
    <ActionConfirmation confirmation={confirmation} onClose={() => setConfirmation(null)} />
  </div>;
}

function TenantForm({ organization = false }: { organization?: boolean }) {
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const action = useAdminAction(() => { setName(""); setSlug(""); });
  return <form className="space-y-3 rounded border p-4" onSubmit={(event) => { event.preventDefault(); action.mutate({ path: organization ? "/administration/organizations" : "/administration/projects", method: "POST", body: { name, slug } }); }}>
    <h2 className="font-medium">Create {organization ? "organization" : "project in current organization"}</h2>
    <Label htmlFor={organization ? "org-name" : "project-name"}>Name</Label><Input id={organization ? "org-name" : "project-name"} value={name} onChange={(event) => setName(event.target.value)} maxLength={255} required />
    <Label htmlFor={organization ? "org-slug" : "project-slug"}>Slug</Label><Input id={organization ? "org-slug" : "project-slug"} value={slug} onChange={(event) => setSlug(event.target.value)} pattern="[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?" maxLength={63} required />
    <Button disabled={action.isPending}>Create {organization ? "organization" : "project"}</Button>
  </form>;
}

export function OrganizationsAdmin() {
  const auth = useAuth();
  const platformAdmin = useCan("platform.manage");
  const currentOrg = auth?.organizations?.find((org) => org.id === auth.context?.organization_id);
  const members = useQuery({ queryKey: ["admin", "members", currentOrg?.id], queryFn: () => api.get<Memberships>("/administration/members") });
  const [subject, setSubject] = useState("");
  const [projectId, setProjectId] = useState("");
  const [role, setRole] = useState("viewer");
  const [confirmation, setConfirmation] = useState<Confirmation | null>(null);
  const [rename, setRename] = useState<{ path: string; name: string } | null>(null);
  const action = useAdminAction(() => { setSubject(""); setRename(null); });
  return <div className="space-y-6">
    <PageHeader eyebrow="Administration" title="Organizations & projects" description="Organization roles apply across every project. Use the member role with explicit project grants to restrict access to selected projects." />
    {auth && <ContextSelector session={auth} />}
    <h2 className="text-lg font-semibold">{currentOrg?.name}</h2>
    <p className="text-sm text-muted-foreground">New resources inherit the selected project. Existing resources without a project remain organization-wide. Project access covers the whole assigned cluster, including its Kubernetes namespaces.</p>
    <div className="flex flex-wrap gap-2"><Button variant="outline" onClick={() => setRename({ path: "/administration/organization", name: currentOrg?.name ?? "" })}>Rename organization</Button></div>
    {rename && <form className="flex gap-2" onSubmit={(event) => { event.preventDefault(); action.mutate({ path: rename.path, method: "PUT", body: { name: rename.name } }); }}><Input aria-label="New name" required maxLength={255} value={rename.name} onChange={(event) => setRename({ ...rename, name: event.target.value })} /><Button disabled={action.isPending}>Save name</Button><Button type="button" variant="outline" onClick={() => setRename(null)}>Cancel</Button></form>}
    <div className="grid gap-4 md:grid-cols-2">{platformAdmin && <TenantForm organization />}<TenantForm /></div>
    <Card><CardContent><h2 className="mb-3 font-semibold">Projects</h2><div className="space-y-3">{currentOrg?.projects.map((project) => <div key={project.id} className="flex flex-wrap items-center justify-between gap-2 border-b pb-3"><span>{project.name} <span className="font-mono text-xs text-muted-foreground">{project.slug}</span></span><div className="flex gap-2"><Button size="sm" variant="outline" onClick={() => setRename({ path: `/administration/projects/${project.id}`, name: project.name })}>Rename</Button>{project.slug !== "default" && <Button size="sm" variant="outline" onClick={() => setConfirmation({ title: `Delete empty project ${project.name}`, subject: project.slug, command: { path: `/administration/projects/${project.id}`, method: "DELETE", query: { confirm: project.slug } } })}>Delete</Button>}</div></div>)}</div></CardContent></Card>
    <form className="space-y-3 rounded border p-4" onSubmit={(event) => { event.preventDefault(); action.mutate({ path: projectId ? `/administration/projects/${projectId}/members` : "/administration/members", method: "PUT", body: { subject: subject.trim(), role } }); }}>
      <h2 className="font-semibold">Grant or update membership</h2>
      <p className="text-sm text-muted-foreground">The user must sign in once. For project-only access, first grant organization membership with the member role, then grant an operator or viewer role on the project.</p>
      <Label htmlFor="identity-subject">Identity subject (issuer|sub)</Label><Input id="identity-subject" required value={subject} onChange={(event) => setSubject(event.target.value)} maxLength={512} />
      <div className="flex flex-wrap gap-3"><label className="space-x-2 text-sm">Scope <select className="rounded border bg-background p-2" value={projectId} onChange={(event) => { setProjectId(event.target.value); setRole("viewer"); }}><option value="">Entire organization</option>{currentOrg?.projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}</select></label><label className="space-x-2 text-sm">Role <select className="rounded border bg-background p-2" value={role} onChange={(event) => setRole(event.target.value)}>{(projectId ? ["operator", "viewer"] : ["org-admin", "operator", "viewer", "member"]).map((value) => <option key={value}>{value}</option>)}</select></label></div>
      <Button disabled={action.isPending}>Save membership</Button>
    </form>
    {members.isError && <ErrorState error={members.error} onRetry={() => members.refetch()} />}
    <Card><CardContent><Table><TableHeader><TableRow><TableHead>User</TableHead><TableHead>Scope</TableHead><TableHead>Role</TableHead><TableHead>Actions</TableHead></TableRow></TableHeader><TableBody>
      {members.data?.organization.map((member) => <TableRow key={member.user.id}><TableCell><p>{member.user.display_name}</p><p className="max-w-lg break-all font-mono text-xs text-muted-foreground">{member.user.subject}</p></TableCell><TableCell>Organization</TableCell><TableCell>{member.role}</TableCell><TableCell><Button size="sm" variant="outline" onClick={() => setConfirmation({ title: "Remove organization membership", subject: member.user.subject, command: { path: `/administration/members/${member.user.id}`, method: "DELETE" } })}>Remove</Button></TableCell></TableRow>)}
      {members.data?.projects.map((member) => <TableRow key={`${member.project_id}-${member.user.id}`}><TableCell>{member.user.display_name}</TableCell><TableCell>{member.project_name}</TableCell><TableCell>{member.role}</TableCell><TableCell><Button size="sm" variant="outline" onClick={() => setConfirmation({ title: "Remove project membership", subject: member.user.subject, command: { path: `/administration/projects/${member.project_id}/members/${member.user.id}`, method: "DELETE" } })}>Remove</Button></TableCell></TableRow>)}
    </TableBody></Table></CardContent></Card>
    <ActionConfirmation confirmation={confirmation} onClose={() => setConfirmation(null)} />
  </div>;
}

export function PermissionsAdmin() {
  const auth = useAuth();
  const policy = useQuery({ queryKey: ["admin", "permissions"], queryFn: () => api.get<{ roles: Record<string, string[]>; effective_permissions: string[] }>("/administration/permissions") });
  return <div className="space-y-6"><PageHeader eyebrow="Administration" title="Permissions" description="Application permissions for the current organization and project. Identity-provider role claims do not grant platform permissions." />
    {auth && <ContextSelector session={auth} />}
    <Card><CardContent><p className="font-medium">Current roles: {auth?.context?.roles.join(", ")}</p><p className="mt-2 break-words font-mono text-xs text-muted-foreground">{policy.data?.effective_permissions.join(", ")}</p></CardContent></Card>
    {policy.isError && <ErrorState error={policy.error} onRetry={() => policy.refetch()} />}
    <Table><TableHeader><TableRow><TableHead>Role</TableHead><TableHead>Permissions</TableHead></TableRow></TableHeader><TableBody>{Object.entries(policy.data?.roles ?? {}).map(([name, values]) => <TableRow key={name}><TableCell>{name}</TableCell><TableCell className="whitespace-normal font-mono text-xs">{values.join(", ")}</TableCell></TableRow>)}</TableBody></Table>
    <p className="text-sm text-muted-foreground">A project operator can administer the assigned Kubernetes cluster. This does not provide namespace-level Kubernetes isolation. Application permissions and Kubernetes RoleBindings are separate controls.</p>
  </div>;
}
