"use client";

import { AlertTriangle, Check, CheckCircle2, ChevronLeft, ChevronRight, Info, Plus, Rocket, Trash2, X } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useMemo, useState } from "react";
import { toast } from "sonner";

import { ErrorState, PageHeader } from "@/components/common/page";
import { ImplementationBadge } from "@/components/common/status";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { MachineStateBadge, primaryAddress } from "@/features/infrastructure/machines";
import { ApiError } from "@/lib/api/client";
import {
  useCreateCluster,
  useDistributions,
  useKubernetesVersions,
  useMachines,
  useProviders,
  useValidateCluster,
} from "@/lib/api/hooks";
import type { ClusterSpecInput, DistributionDescriptor, Machine, Taint } from "@/lib/api/types";
import { humanize } from "@/lib/format";
import { cn } from "@/lib/utils";

// ---------------------------------------------------------------------------- model

const DNS_LABEL = /^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$/;
const CIDR = /^(\d{1,3}\.){3}\d{1,3}\/\d{1,2}$/;
const VERSION = /^1\.\d{1,2}(\.\d{1,3})?$/;
const LABEL_KEY = /^([a-z0-9]([-a-z0-9.]*[a-z0-9])?\/)?[A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$/;

interface KeyValue {
  key: string;
  value: string;
}
interface PoolState {
  name: string;
  machines: string[];
  labels: KeyValue[];
  taints: Taint[];
}
interface WizardState {
  provider: string;
  distribution: string;
  name: string;
  description: string;
  version: string;
  cni: string;
  podCidr: string;
  serviceCidr: string;
  dnsDomain: string;
  cpMachines: string[];
  cpEndpoint: string;
  pools: PoolState[];
  allowSwap: boolean;
  cpWorkloads: "auto" | "yes" | "no";
  conntrackZero: boolean;
  ignorePreflight: string;
  resolvConf: string;
}

const INITIAL: WizardState = {
  provider: "",
  distribution: "",
  name: "",
  description: "",
  version: "",
  cni: "",
  podCidr: "10.244.0.0/16",
  serviceCidr: "10.96.0.0/12",
  dnsDomain: "cluster.local",
  cpMachines: [],
  cpEndpoint: "",
  pools: [{ name: "workers", machines: [], labels: [], taints: [] }],
  allowSwap: false,
  cpWorkloads: "auto",
  conntrackZero: false,
  ignorePreflight: "",
  resolvConf: "",
};

function buildSpec(s: WizardState): ClusterSpecInput {
  const ha = s.cpMachines.length > 1;
  // Only explicit choices are sent: the backend fills in defaults (e.g. for container-based machines).
  const options: Record<string, unknown> = {};
  if (s.allowSwap) options.allow_swap = true;
  if (s.cpWorkloads !== "auto") options.control_plane_workloads = s.cpWorkloads === "yes";
  if (s.conntrackZero) options.kube_proxy_conntrack_max_per_core = 0;
  if (s.resolvConf.trim()) options.kubelet_resolv_conf = s.resolvConf.trim();
  const preflight = s.ignorePreflight.split(",").map((v) => v.trim()).filter(Boolean);
  if (preflight.length) options.ignore_preflight_errors = preflight;
  return {
    name: s.name,
    distribution: s.distribution,
    provider_type: s.provider,
    kubernetes_version: s.version,
    container_runtime: "containerd",
    network: { pod_cidr: s.podCidr, service_cidr: s.serviceCidr, dns_domain: s.dnsDomain, cni: s.cni },
    control_plane: {
      name: "control-plane",
      count: s.cpMachines.length,
      existing_machine_ids: s.cpMachines,
      endpoint: ha ? s.cpEndpoint.trim() : null,
      load_balancer: ha ? "external" : "none",
    },
    worker_pools: s.pools
      .filter((p) => p.machines.length > 0)
      .map((p) => ({
        name: p.name,
        count: p.machines.length,
        existing_machine_ids: p.machines,
        labels: Object.fromEntries(p.labels.filter((l) => l.key).map((l) => [l.key, l.value])),
        taints: p.taints.filter((t) => t.key),
      })),
    options,
  };
}

// ---------------------------------------------------------------------------- shared bits

function ChoiceCard({
  selected,
  disabled,
  onSelect,
  title,
  badge,
  description,
  reason,
}: {
  selected: boolean;
  disabled?: boolean;
  onSelect: () => void;
  title: string;
  badge?: React.ReactNode;
  description: string;
  reason?: string;
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onSelect}
      aria-pressed={selected}
      className={cn(
        "flex flex-col gap-2 rounded-lg border p-4 text-left transition-colors",
        selected ? "border-sky-500/60 bg-sky-500/10" : "border-border hover:border-foreground/30",
        disabled && "cursor-not-allowed opacity-50 hover:border-border",
      )}
    >
      <div className="flex items-start justify-between gap-2">
        <span className="font-medium">{title}</span>
        {selected ? <CheckCircle2 className="size-4 text-sky-400" /> : badge}
      </div>
      <span className="text-sm text-muted-foreground">{description}</span>
      {reason && <span className="text-xs text-amber-300/90">{reason}</span>}
    </button>
  );
}

function Field({ label, hint, error, children }: { label: string; hint?: string; error?: string | null; children: React.ReactNode }) {
  return (
    <div className="space-y-1.5">
      <Label>{label}</Label>
      {children}
      {(error || hint) && <p className={cn("text-xs", error ? "text-red-400" : "text-muted-foreground")}>{error ?? hint}</p>}
    </div>
  );
}

function MachinePicker({
  machines,
  selected,
  onChange,
  takenElsewhere,
  requireTwoCpus,
}: {
  machines: Machine[];
  selected: string[];
  onChange: (ids: string[]) => void;
  takenElsewhere: Set<string>;
  requireTwoCpus?: boolean;
}) {
  if (!machines.length) {
    return (
      <p className="rounded-md border border-dashed border-border p-4 text-sm text-muted-foreground">
        No machines registered.{" "}
        <Link href="/infrastructure/machines" className="text-foreground underline underline-offset-4">Register machines</Link>{" "}
        first.
      </p>
    );
  }
  return (
    <ul className="divide-y divide-border/60 rounded-md border border-border">
      {machines.map((m) => {
        const isSelected = selected.includes(m.id);
        const reason =
          m.cluster_name ? `In cluster ${m.cluster_name}`
          : m.lifecycle_state !== "ready" ? (m.last_error ?? "Not ready — inspect it on the Machines page")
          : takenElsewhere.has(m.id) ? "Selected in another node pool"
          : requireTwoCpus && (m.hardware.cpu_cores ?? 0) < 2 ? "Control-plane nodes need at least 2 CPUs"
          : null;
        const disabled = !isSelected && reason !== null;
        return (
          <li key={m.id}>
            <label className={cn("flex items-center gap-3 px-3 py-2.5", disabled ? "opacity-50" : "cursor-pointer hover:bg-muted/30")}>
              <input
                type="checkbox"
                checked={isSelected}
                disabled={disabled}
                onChange={(e) => onChange(e.target.checked ? [...selected, m.id] : selected.filter((id) => id !== m.id))}
              />
              <div className="min-w-0 flex-1">
                <p className="text-sm font-medium">
                  {m.name} <span className="font-mono text-xs font-normal text-muted-foreground">{primaryAddress(m) ?? m.ssh_host}</span>
                </p>
                <p className="truncate text-xs text-muted-foreground">
                  {m.os_info.pretty_name ?? "Unknown OS"}
                  {m.hardware.cpu_cores ? ` · ${m.hardware.cpu_cores} CPU · ${Math.round(((m.hardware.memory_mb ?? 0) / 1024) * 10) / 10} GB` : ""}
                  {reason && !isSelected ? ` — ${reason}` : ""}
                </p>
              </div>
              <MachineStateBadge state={m.lifecycle_state} />
            </label>
          </li>
        );
      })}
    </ul>
  );
}

// ---------------------------------------------------------------------------- steps

const STEPS = [
  { key: "infrastructure", title: "Infrastructure" },
  { key: "distribution", title: "Distribution" },
  { key: "cluster", title: "Cluster" },
  { key: "control-plane", title: "Control plane" },
  { key: "workers", title: "Worker nodes" },
  { key: "storage", title: "Storage & advanced" },
  { key: "review", title: "Review & create" },
] as const;

function stepErrors(step: number, s: WizardState, distribution?: DistributionDescriptor): string[] {
  const errors: string[] = [];
  if (step === 0 && !s.provider) errors.push("Choose an infrastructure provider");
  if (step === 1 && !s.distribution) errors.push("Choose a Kubernetes distribution");
  if (step === 2) {
    if (!DNS_LABEL.test(s.name)) errors.push("Name: lowercase letters, digits and '-' (max 63)");
    if (!VERSION.test(s.version)) errors.push("Choose a Kubernetes version (e.g. 1.34)");
    if (!CIDR.test(s.podCidr)) errors.push("Pod CIDR must look like 10.244.0.0/16");
    if (!CIDR.test(s.serviceCidr)) errors.push("Service CIDR must look like 10.96.0.0/12");
    if (!s.cni || (distribution && !distribution.supported_cnis.includes(s.cni))) errors.push("Choose a pod network");
  }
  if (step === 3) {
    const n = s.cpMachines.length;
    if (n === 0) errors.push("Select at least one control-plane machine");
    if (n > 1 && n % 2 === 0) errors.push("Highly available control planes need an odd number of machines (3 or 5)");
    if (n > 1 && !s.cpEndpoint.trim()) errors.push("Provide the load balancer address for the API server");
  }
  if (step === 4) {
    const names = s.pools.map((p) => p.name);
    if (new Set(names).size !== names.length) errors.push("Node pool names must be unique");
    for (const p of s.pools) {
      if (!DNS_LABEL.test(p.name) || p.name === "control-plane") errors.push(`Invalid pool name "${p.name}"`);
      for (const l of p.labels) if (l.key && !LABEL_KEY.test(l.key)) errors.push(`Invalid label key "${l.key}"`);
      for (const t of p.taints) if (t.key && !LABEL_KEY.test(t.key)) errors.push(`Invalid taint key "${t.key}"`);
    }
  }
  return errors;
}

function KeyValueEditor({
  items,
  onChange,
  placeholder,
}: {
  items: KeyValue[];
  onChange: (items: KeyValue[]) => void;
  placeholder: [string, string];
}) {
  return (
    <div className="space-y-2">
      {items.map((item, i) => (
        <div key={i} className="flex gap-2">
          <Input placeholder={placeholder[0]} value={item.key} onChange={(e) => onChange(items.map((x, j) => (j === i ? { ...x, key: e.target.value } : x)))} />
          <Input placeholder={placeholder[1]} value={item.value} onChange={(e) => onChange(items.map((x, j) => (j === i ? { ...x, value: e.target.value } : x)))} />
          <Button variant="ghost" size="icon" aria-label="Remove" onClick={() => onChange(items.filter((_, j) => j !== i))}><X /></Button>
        </div>
      ))}
      <Button variant="ghost" size="xs" onClick={() => onChange([...items, { key: "", value: "" }])}><Plus /> Add label</Button>
    </div>
  );
}

function TaintEditor({ items, onChange }: { items: Taint[]; onChange: (items: Taint[]) => void }) {
  return (
    <div className="space-y-2">
      {items.map((t, i) => (
        <div key={i} className="flex gap-2">
          <Input placeholder="key" value={t.key} onChange={(e) => onChange(items.map((x, j) => (j === i ? { ...x, key: e.target.value } : x)))} />
          <Input placeholder="value (optional)" value={t.value ?? ""} onChange={(e) => onChange(items.map((x, j) => (j === i ? { ...x, value: e.target.value } : x)))} />
          <Select value={t.effect} onValueChange={(v) => onChange(items.map((x, j) => (j === i ? { ...x, effect: v as Taint["effect"] } : x)))}>
            <SelectTrigger className="w-44"><SelectValue /></SelectTrigger>
            <SelectContent>
              {(["NoSchedule", "PreferNoSchedule", "NoExecute"] as const).map((e) => <SelectItem key={e} value={e}>{e}</SelectItem>)}
            </SelectContent>
          </Select>
          <Button variant="ghost" size="icon" aria-label="Remove" onClick={() => onChange(items.filter((_, j) => j !== i))}><X /></Button>
        </div>
      ))}
      <Button variant="ghost" size="xs" onClick={() => onChange([...items, { key: "", value: "", effect: "NoSchedule" }])}><Plus /> Add taint</Button>
    </div>
  );
}

// ---------------------------------------------------------------------------- wizard

export function CreateClusterWizard() {
  const router = useRouter();
  const providers = useProviders();
  const distributions = useDistributions();
  const versions = useKubernetesVersions();
  const machines = useMachines();
  const validate = useValidateCluster();
  const create = useCreateCluster();

  const [state, setState] = useState<WizardState>(INITIAL);
  const [step, setStep] = useState(0);
  const [visited, setVisited] = useState(0);
  const [customVersion, setCustomVersion] = useState(false);
  const update = (patch: Partial<WizardState>) => setState((s) => ({ ...s, ...patch }));

  const distribution = distributions.data?.find((d) => d.type === state.distribution);
  const machineList = machines.data ?? [];
  const errors = stepErrors(step, state, distribution);
  const spec = useMemo(() => buildSpec(state), [state]);

  const workerIds = new Set(state.pools.flatMap((p) => p.machines));
  const cpIds = new Set(state.cpMachines);

  const goTo = (target: number) => {
    setStep(target);
    setVisited((v) => Math.max(v, target));
    if (target === STEPS.length - 1) validate.mutate(spec);
  };

  const submit = () =>
    create.mutate(
      { spec, description: state.description || undefined },
      {
        onSuccess: ({ cluster, operation }) => {
          toast.success(`Creating cluster ${cluster.name}`, { description: "Installation is running; follow it live." });
          router.push(`/operations/${operation.id}`);
        },
        onError: (e) => {
          toast.error("Could not create cluster", { description: e.message });
          if (e instanceof ApiError && e.status === 422) validate.mutate(spec);
        },
      },
    );

  if (providers.isLoading || distributions.isLoading || machines.isLoading) {
    return <div className="space-y-4"><Skeleton className="h-16" /><Skeleton className="h-96" /></div>;
  }
  const loadError = providers.error ?? distributions.error ?? machines.error;
  if (loadError) return <ErrorState error={loadError} onRetry={() => { void providers.refetch(); void distributions.refetch(); void machines.refetch(); }} />;

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow="Kubernetes"
        title="Create cluster"
        description="Install Kubernetes on registered machines. Every step runs as a tracked, resumable operation."
      />
      <div className="grid gap-6 lg:grid-cols-[14rem_1fr]">
        <nav aria-label="Wizard steps">
          <ol className="space-y-1">
            {STEPS.map((s, i) => (
              <li key={s.key}>
                <button
                  type="button"
                  disabled={i > visited}
                  onClick={() => goTo(i)}
                  className={cn(
                    "flex w-full items-center gap-3 rounded-md px-3 py-2 text-left text-sm",
                    i === step ? "bg-muted font-medium" : "text-muted-foreground hover:text-foreground",
                    i > visited && "cursor-not-allowed opacity-50 hover:text-muted-foreground",
                  )}
                >
                  <span
                    className={cn(
                      "flex size-6 items-center justify-center rounded-full border text-xs",
                      i < step ? "border-emerald-500/50 bg-emerald-500/15 text-emerald-300" : i === step ? "border-sky-500/60 text-sky-300" : "border-border",
                    )}
                  >
                    {i < step ? <Check className="size-3.5" /> : i + 1}
                  </span>
                  {s.title}
                </button>
              </li>
            ))}
          </ol>
        </nav>

        <Card>
          <CardContent className="space-y-6">
            {step === 0 && (
              <section className="space-y-4">
                <div>
                  <h2 className="font-semibold">Where should the cluster run?</h2>
                  <p className="text-sm text-muted-foreground">The infrastructure provider supplies the machines.</p>
                </div>
                <div className="grid gap-3 md:grid-cols-2">
                  {providers.data?.map((p) => (
                    <ChoiceCard
                      key={p.type}
                      title={p.display_name}
                      description={p.description}
                      badge={<ImplementationBadge status={p.implementation_status} phase={p.planned_phase} />}
                      selected={state.provider === p.type}
                      disabled={p.implementation_status === "planned"}
                      onSelect={() => update({ provider: p.type, distribution: "" })}
                    />
                  ))}
                </div>
              </section>
            )}

            {step === 1 && (
              <section className="space-y-4">
                <div>
                  <h2 className="font-semibold">Kubernetes distribution</h2>
                  <p className="text-sm text-muted-foreground">How Kubernetes is installed and operated.</p>
                </div>
                <div className="grid gap-3 md:grid-cols-2">
                  {distributions.data?.map((d) => {
                    const supported = d.supported_providers.includes(state.provider);
                    const planned = d.implementation_status === "planned";
                    return (
                      <ChoiceCard
                        key={d.type}
                        title={d.display_name}
                        description={d.description}
                        badge={<ImplementationBadge status={d.implementation_status} phase={d.planned_phase} />}
                        selected={state.distribution === d.type}
                        disabled={planned || !supported}
                        reason={!planned && !supported ? `Not available on ${humanize(state.provider)}` : undefined}
                        onSelect={() => update({ distribution: d.type, cni: d.default_cni })}
                      />
                    );
                  })}
                </div>
              </section>
            )}

            {step === 2 && distribution && (
              <section className="space-y-5">
                <h2 className="font-semibold">Cluster settings</h2>
                <div className="grid gap-4 md:grid-cols-2">
                  <Field label="Cluster name" hint="Lowercase letters, digits and '-'." error={state.name && !DNS_LABEL.test(state.name) ? "Invalid name" : null}>
                    <Input value={state.name} onChange={(e) => update({ name: e.target.value })} placeholder="production" />
                  </Field>
                  <Field
                    label="Kubernetes version"
                    hint={
                      versions.data?.available
                        ? `Latest patch release is installed. Minimum supported: ${versions.data.minimum_minor}.`
                        : (versions.data?.message ?? "Loading releases…")
                    }
                  >
                    {versions.data?.available && !customVersion ? (
                      <Select
                        value={state.version}
                        onValueChange={(v) => (v === "custom" ? setCustomVersion(true) : update({ version: v }))}
                      >
                        <SelectTrigger className="w-full"><SelectValue placeholder="Select a version" /></SelectTrigger>
                        <SelectContent>
                          {versions.data.versions.map((v, i) => (
                            <SelectItem key={v.minor} value={v.minor}>
                              {v.minor} (v{v.latest_patch}){i === 0 ? " — latest" : ""}
                            </SelectItem>
                          ))}
                          <SelectItem value="custom">Other version…</SelectItem>
                        </SelectContent>
                      </Select>
                    ) : (
                      <Input value={state.version} onChange={(e) => update({ version: e.target.value.replace(/^v/, "") })} placeholder="1.34 or 1.34.2" />
                    )}
                  </Field>
                </div>
                <Field label="Description (optional)">
                  <Textarea value={state.description} onChange={(e) => update({ description: e.target.value })} className="min-h-16" />
                </Field>
                <div className="grid gap-4 md:grid-cols-3">
                  <Field label="Pod network (CNI)">
                    <Select value={state.cni} onValueChange={(v) => update({ cni: v })}>
                      <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
                      <SelectContent>
                        {distribution.supported_cnis.map((c) => <SelectItem key={c} value={c}>{humanize(c)}</SelectItem>)}
                      </SelectContent>
                    </Select>
                  </Field>
                  <Field label="Pod CIDR" error={state.podCidr && !CIDR.test(state.podCidr) ? "Invalid CIDR" : null}>
                    <Input value={state.podCidr} onChange={(e) => update({ podCidr: e.target.value })} className="font-mono" />
                  </Field>
                  <Field label="Service CIDR" error={state.serviceCidr && !CIDR.test(state.serviceCidr) ? "Invalid CIDR" : null}>
                    <Input value={state.serviceCidr} onChange={(e) => update({ serviceCidr: e.target.value })} className="font-mono" />
                  </Field>
                  <Field label="Cluster DNS domain">
                    <Input value={state.dnsDomain} onChange={(e) => update({ dnsDomain: e.target.value })} className="font-mono" />
                  </Field>
                  <Field label="Container runtime" hint="Docker Engine is not required.">
                    <Input value={distribution.supported_runtimes.join(", ")} disabled />
                  </Field>
                </div>
              </section>
            )}

            {step === 3 && (
              <section className="space-y-4">
                <div>
                  <h2 className="font-semibold">Control plane</h2>
                  <p className="text-sm text-muted-foreground">
                    One machine for a single control plane, or 3/5 for high availability behind your load balancer.
                  </p>
                </div>
                <MachinePicker
                  machines={machineList}
                  selected={state.cpMachines}
                  onChange={(ids) => update({ cpMachines: ids })}
                  takenElsewhere={workerIds}
                  requireTwoCpus
                />
                {state.cpMachines.length > 1 && (
                  <Field
                    label="API server endpoint (load balancer)"
                    hint="host[:port] of an existing TCP load balancer or DNS name in front of port 6443 on all control-plane machines."
                  >
                    <Input value={state.cpEndpoint} onChange={(e) => update({ cpEndpoint: e.target.value })} placeholder="k8s-api.example.internal:6443" className="font-mono" />
                  </Field>
                )}
              </section>
            )}

            {step === 4 && (
              <section className="space-y-4">
                <div className="flex items-end justify-between gap-4">
                  <div>
                    <h2 className="font-semibold">Worker nodes</h2>
                    <p className="text-sm text-muted-foreground">
                      Group workers into pools with shared labels and taints. With no workers, the control plane runs workloads.
                    </p>
                  </div>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => update({ pools: [...state.pools, { name: `pool-${state.pools.length + 1}`, machines: [], labels: [], taints: [] }] })}
                  >
                    <Plus /> Add pool
                  </Button>
                </div>
                {state.pools.map((pool, index) => {
                  const otherPools = new Set(state.pools.filter((_, i) => i !== index).flatMap((p) => p.machines));
                  const setPool = (patch: Partial<PoolState>) =>
                    update({ pools: state.pools.map((p, i) => (i === index ? { ...p, ...patch } : p)) });
                  return (
                    <div key={index} className="space-y-3 rounded-lg border border-border p-4">
                      <div className="flex items-end gap-3">
                        <Field label="Pool name">
                          <Input value={pool.name} onChange={(e) => setPool({ name: e.target.value })} className="w-56" />
                        </Field>
                        <span className="pb-2 text-sm text-muted-foreground">{pool.machines.length} machine(s)</span>
                        {state.pools.length > 1 && (
                          <Button variant="ghost" size="sm" className="ml-auto" onClick={() => update({ pools: state.pools.filter((_, i) => i !== index) })}>
                            <Trash2 /> Remove pool
                          </Button>
                        )}
                      </div>
                      <MachinePicker
                        machines={machineList}
                        selected={pool.machines}
                        onChange={(ids) => setPool({ machines: ids })}
                        takenElsewhere={new Set([...cpIds, ...otherPools])}
                      />
                      <div className="grid gap-4 md:grid-cols-2">
                        <Field label="Labels">
                          <KeyValueEditor items={pool.labels} onChange={(labels) => setPool({ labels })} placeholder={["key", "value"]} />
                        </Field>
                        <Field label="Taints">
                          <TaintEditor items={pool.taints} onChange={(taints) => setPool({ taints })} />
                        </Field>
                      </div>
                    </div>
                  );
                })}
              </section>
            )}

            {step === 5 && (
              <section className="space-y-5">
                <div className="flex gap-3 rounded-lg border border-sky-500/20 bg-sky-500/5 p-4 text-sm">
                  <Info className="mt-0.5 size-4 shrink-0 text-sky-400" />
                  <div className="space-y-1">
                    <p className="font-medium">Storage</p>
                    <p className="text-muted-foreground">
                      No storage provisioner is installed in this release: workloads that request persistent volumes need a
                      CSI driver. Storage classes and CSI driver installation arrive in Phase 6.
                    </p>
                  </div>
                </div>
                <div className="space-y-4">
                  <h2 className="font-semibold">Advanced options</h2>
                  <label className="flex items-start gap-3 text-sm">
                    <input type="checkbox" className="mt-1" checked={state.allowSwap} onChange={(e) => update({ allowSwap: e.target.checked })} />
                    <span>
                      <span className="font-medium">Leave swap enabled</span>
                      <span className="block text-muted-foreground">By default swap is disabled on every node. Enable this only if swap must stay on (kubelet runs with failSwapOn=false).</span>
                    </span>
                  </label>
                  <label className="flex items-start gap-3 text-sm">
                    <input type="checkbox" className="mt-1" checked={state.conntrackZero} onChange={(e) => update({ conntrackZero: e.target.checked })} />
                    <span>
                      <span className="font-medium">Don&apos;t let kube-proxy change conntrack limits</span>
                      <span className="block text-muted-foreground">Needed when /proc/sys is read-only (for example container-based test nodes).</span>
                    </span>
                  </label>
                  <div className="grid gap-4 md:grid-cols-2">
                    <Field label="Workloads on control-plane nodes">
                      <Select value={state.cpWorkloads} onValueChange={(v) => update({ cpWorkloads: v as WizardState["cpWorkloads"] })}>
                        <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
                        <SelectContent>
                          <SelectItem value="auto">Automatic (only when there are no workers)</SelectItem>
                          <SelectItem value="yes">Allow</SelectItem>
                          <SelectItem value="no">Never</SelectItem>
                        </SelectContent>
                      </Select>
                    </Field>
                    <Field label="Ignore kubeadm preflight checks" hint="Comma-separated check names, e.g. Swap. Use with care.">
                      <Input value={state.ignorePreflight} onChange={(e) => update({ ignorePreflight: e.target.value })} className="font-mono" />
                    </Field>
                    <Field
                      label="Kubelet resolv.conf (optional)"
                      hint="Only when machines use a loopback DNS resolver (see the machine's DNS check): a file on every node listing real upstream nameservers."
                    >
                      <Input value={state.resolvConf} onChange={(e) => update({ resolvConf: e.target.value })} placeholder="/etc/kubernetes/resolv.conf" className="font-mono" />
                    </Field>
                  </div>
                </div>
              </section>
            )}

            {step === 6 && (
              <section className="space-y-5">
                <h2 className="font-semibold">Review</h2>
                <dl className="grid gap-x-8 gap-y-2 text-sm md:grid-cols-2">
                  {(
                    [
                      ["Cluster", state.name],
                      ["Provider / distribution", `${humanize(state.provider)} · ${distribution?.display_name}`],
                      ["Kubernetes", state.version],
                      ["Network", `${state.cni} · pods ${state.podCidr} · services ${state.serviceCidr}`],
                      ["Control plane", `${state.cpMachines.length} node(s)${state.cpMachines.length > 1 ? ` behind ${state.cpEndpoint}` : ""}`],
                      ["Workers", `${spec.worker_pools.reduce((n, p) => n + p.count, 0)} node(s) in ${spec.worker_pools.length} pool(s)`],
                    ] as const
                  ).map(([label, value]) => (
                    <div key={label} className="flex justify-between gap-4 border-b border-border/40 pb-1.5">
                      <dt className="text-muted-foreground">{label}</dt>
                      <dd className="text-right">{value}</dd>
                    </div>
                  ))}
                </dl>
                {validate.isPending && <Skeleton className="h-40" />}
                {validate.isError && <ErrorState error={validate.error} onRetry={() => validate.mutate(spec)} />}
                {validate.data && !validate.data.valid && (
                  <div className="space-y-2 rounded-lg border border-red-500/30 bg-red-500/5 p-4">
                    <p className="flex items-center gap-2 text-sm font-medium text-red-300"><AlertTriangle className="size-4" /> This specification cannot be installed yet</p>
                    <ul className="list-disc space-y-1 pl-6 text-sm text-red-200/90">
                      {validate.data.problems.map((p) => <li key={p}>{p}</li>)}
                    </ul>
                  </div>
                )}
                {validate.data && validate.data.notes.length > 0 && (
                  <ul className="space-y-1 rounded-lg border border-sky-500/30 bg-sky-500/5 p-3 text-sm text-sky-200/90">
                    {validate.data.notes.map((n) => <li key={n}>{n}</li>)}
                  </ul>
                )}
                {validate.data?.valid && (
                  <div className="space-y-3">
                    <p className="flex items-center gap-2 text-sm text-emerald-300"><CheckCircle2 className="size-4" /> Validated against the current inventory. The installation will run these steps:</p>
                    <ol className="space-y-1 rounded-lg border border-border p-3 text-sm">
                      {validate.data.steps.map((s, i) => (
                        <li key={s.key} className="flex gap-3">
                          <span className="w-6 text-right tabular-nums text-muted-foreground">{i + 1}.</span>
                          {s.name}
                        </li>
                      ))}
                    </ol>
                    <p className="text-xs text-muted-foreground">
                      Packages come from pkgs.k8s.io and download.docker.com; images from registry.k8s.io. Nodes need outbound
                      internet access. Swap is {state.allowSwap || validate.data.notes.some((n) => n.includes("allow_swap")) ? "left enabled" : "disabled"} on every node.
                    </p>
                  </div>
                )}
              </section>
            )}

            {errors.length > 0 && step < STEPS.length - 1 && (
              <ul className="space-y-1 text-sm text-amber-300/90">
                {errors.map((e) => <li key={e}>• {e}</li>)}
              </ul>
            )}

            <div className="flex justify-between border-t border-border/60 pt-4">
              <Button variant="outline" disabled={step === 0} onClick={() => goTo(step - 1)}>
                <ChevronLeft /> Back
              </Button>
              {step < STEPS.length - 1 ? (
                <Button disabled={errors.length > 0} onClick={() => goTo(step + 1)}>
                  Next <ChevronRight />
                </Button>
              ) : (
                <Button disabled={!validate.data?.valid || create.isPending} onClick={submit}>
                  <Rocket /> {create.isPending ? "Starting…" : "Create cluster"}
                </Button>
              )}
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
