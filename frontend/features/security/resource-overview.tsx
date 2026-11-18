import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { tokenMountSetting } from "@/features/workloads/kinds";
import type { ResourceDetail } from "@/lib/api/workloads";

type PolicyRule = {
  verbs: string[];
  apiGroups?: string[];
  resources?: string[];
  resourceNames?: string[];
  nonResourceURLs?: string[];
};

type Subject = { kind: string; name: string; namespace?: string };

function Rules({ detail }: { detail: ResourceDetail }) {
  const rules = (detail.object.rules ?? []) as PolicyRule[];
  return (
    <section className="space-y-2">
      <h3 className="text-sm font-medium">Permission rules</h3>
      {Boolean(detail.object.aggregationRule) && (
        <p className="text-sm text-muted-foreground">Kubernetes combines rules from matching ClusterRoles. See YAML for the aggregation selectors.</p>
      )}
      {rules.length ? (
        <Table>
          <TableHeader><TableRow>
            <TableHead>Verbs</TableHead><TableHead>API groups</TableHead><TableHead>Resources</TableHead>
            <TableHead>Resource names</TableHead><TableHead>Non-resource URLs</TableHead>
          </TableRow></TableHeader>
          <TableBody>
            {rules.map((rule, index) => (
              <TableRow key={index} className="font-mono text-xs">
                <TableCell className="whitespace-normal">{rule.verbs.join(", ")}</TableCell>
                <TableCell className="whitespace-normal">{rule.apiGroups?.map((group) => group || "(core)").join(", ") || "—"}</TableCell>
                <TableCell className="whitespace-normal">{rule.resources?.join(", ") || "—"}</TableCell>
                <TableCell className="whitespace-normal">{rule.resourceNames?.join(", ") || (rule.resources?.length ? "Any" : "—")}</TableCell>
                <TableCell className="whitespace-normal">{rule.nonResourceURLs?.join(", ") || "—"}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      ) : <p className="text-sm text-muted-foreground">No permission rules.</p>}
    </section>
  );
}

function Binding({ detail }: { detail: ResourceDetail }) {
  const subjects = (detail.object.subjects ?? []) as Subject[];
  const reference = detail.object.roleRef as { kind: string; name: string };
  const namespace = detail.summary.namespace;
  return (
    <section className="space-y-2">
      <h3 className="text-sm font-medium">Binding subjects</h3>
      <p className="text-sm text-muted-foreground">
        Grants <span className="font-medium text-foreground">{reference.kind} <code>{reference.name}</code></span>
        {namespace ? <> in namespace <code>{namespace}</code>.</> : " across the cluster."}
      </p>
      {subjects.length ? (
        <Table>
          <TableHeader><TableRow><TableHead>Kind</TableHead><TableHead>Name</TableHead><TableHead>Namespace</TableHead></TableRow></TableHeader>
          <TableBody>
            {subjects.map((subject, index) => (
              <TableRow key={index}>
                <TableCell>{subject.kind}</TableCell>
                <TableCell className="font-mono text-xs">{subject.name}</TableCell>
                <TableCell>{subject.kind === "ServiceAccount" ? subject.namespace || namespace || "—" : "—"}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      ) : <p className="text-sm text-muted-foreground">No subjects bound.</p>}
    </section>
  );
}

function ServiceAccount({ detail }: { detail: ResourceDetail }) {
  const pullSecrets = (detail.object.imagePullSecrets ?? []) as { name: string }[];
  const secrets = (detail.object.secrets ?? []) as { name: string }[];
  return (
    <section className="space-y-2 text-sm">
      <h3 className="font-medium">Identity and credential references</h3>
      <p><code>system:serviceaccount:{detail.summary.namespace}:{detail.summary.name}</code></p>
      <dl className="grid grid-cols-[11rem_1fr] gap-x-3 gap-y-1">
        <dt className="text-muted-foreground">Token automount</dt>
        <dd>{tokenMountSetting(detail.object.automountServiceAccountToken)} <span className="text-muted-foreground">(a pod can override this setting)</span></dd>
        <dt className="text-muted-foreground">Image pull Secret names</dt>
        <dd className="font-mono text-xs">{pullSecrets.map((ref) => ref.name).join(", ") || "—"}</dd>
        <dt className="text-muted-foreground">Other Secret references</dt>
        <dd className="font-mono text-xs">{secrets.map((ref) => ref.name).join(", ") || "—"}</dd>
      </dl>
    </section>
  );
}

function AdmissionPolicy({ detail }: { detail: ResourceDetail }) {
  const spec = detail.object.spec as {
    validations?: { expression: string; message?: string; messageExpression?: string }[];
  };
  const status = detail.object.status as {
    typeChecking?: { expressionWarnings?: { fieldRef: string; warning: string }[] };
  } | undefined;
  const warnings = status?.typeChecking?.expressionWarnings ?? [];
  return (
    <section className="space-y-3 text-sm">
      <h3 className="font-medium">Validation rules</h3>
      <p className="text-muted-foreground">Failure policy <code>{String(detail.summary.failure_policy)}</code>. See YAML for request matching and parameter configuration.</p>
      {spec.validations?.length ? (
        <ol className="space-y-2">
          {spec.validations.map((validation, index) => (
            <li key={index} className="space-y-1 rounded-md border p-3">
              <pre className="whitespace-pre-wrap break-all font-mono text-xs">{validation.expression}</pre>
              {validation.message && <p className="text-muted-foreground">{validation.message}</p>}
              {validation.messageExpression && <p className="text-muted-foreground">Message expression <code className="break-all">{validation.messageExpression}</code></p>}
            </li>
          ))}
        </ol>
      ) : <p className="text-muted-foreground">No validation expressions configured.</p>}
      {warnings.length > 0 && (
        <>
          <h3 className="font-medium">Reported type warnings</h3>
          <ul className="space-y-2">
            {warnings.map((warning, index) => (
              <li key={index} className="rounded-md border p-3">
                <p className="font-mono text-xs">{warning.fieldRef}</p>
                <p className="whitespace-pre-wrap break-words text-muted-foreground">{warning.warning}</p>
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}

function AdmissionBinding({ detail }: { detail: ResourceDetail }) {
  const spec = detail.object.spec as { policyName: string; validationActions: string[] };
  return (
    <section className="space-y-2 text-sm">
      <h3 className="font-medium">Policy binding</h3>
      <dl className="grid grid-cols-[10rem_1fr] gap-x-3 gap-y-1">
        <dt className="text-muted-foreground">Policy</dt>
        <dd><code>{spec.policyName}</code></dd>
        <dt className="text-muted-foreground">Validation actions</dt>
        <dd>{spec.validationActions.join(", ")}</dd>
      </dl>
      <p className="text-muted-foreground">See YAML for the binding’s request filters and parameter reference.</p>
    </section>
  );
}

export function SecurityResourceOverview({ detail }: { detail: ResourceDetail }) {
  switch (detail.kind) {
    case "Role":
    case "ClusterRole":
      return <Rules detail={detail} />;
    case "RoleBinding":
    case "ClusterRoleBinding":
      return <Binding detail={detail} />;
    case "ServiceAccount":
      return <ServiceAccount detail={detail} />;
    case "ValidatingAdmissionPolicy":
      return <AdmissionPolicy detail={detail} />;
    case "ValidatingAdmissionPolicyBinding":
      return <AdmissionBinding detail={detail} />;
    default:
      return null;
  }
}
