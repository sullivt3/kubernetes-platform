"use client";

import type { ReactNode } from "react";

import { StatusPill } from "@/components/common/status";
import { PodStatusBadge } from "@/features/kubernetes/shared";
import type { ResourceKind, ResourceRow, Rollout } from "@/lib/api/workloads";
import { formatAge } from "@/lib/format";

export interface Column {
  header: string;
  cell: (row: ResourceRow) => ReactNode;
  className?: string;
}

export interface KindConfig {
  kind: ResourceKind;
  title: string;
  singular: string;
  eyebrow: string;
  description: string;
  scope?: "namespace" | "cluster";
  columns: Column[];
}

const s = (v: unknown) => (v === null || v === undefined || v === "" ? "—" : String(v));
const mono = (v: unknown) => <span className="font-mono text-xs">{s(v)}</span>;
const list = (v: unknown) => (Array.isArray(v) && v.length ? <span className="font-mono text-xs">{v.join(", ")}</span> : "—");
const age: Column = { header: "Age", cell: (r) => formatAge(r.created), className: "text-right text-muted-foreground" };

export function RolloutBadge({ rollout }: { rollout?: Rollout }) {
  if (!rollout) return <>—</>;
  const tone = { complete: "success", progressing: "info", failed: "danger", paused: "warning" } as const;
  return <StatusPill tone={tone[rollout.state]}>{rollout.state}</StatusPill>;
}

export const KIND_CONFIG: Record<ResourceKind, KindConfig> = {
  pods: {
    kind: "pods", title: "Pods", singular: "Pod", eyebrow: "Workloads",
    description: "Pods across namespaces: status, logs, events and shell access.",
    columns: [
      { header: "Status", cell: (r) => <PodStatusBadge status={s(r.status)} problem={Boolean(r.problem)} phase={s(r.phase)} /> },
      { header: "Ready", cell: (r) => s(r.ready) },
      { header: "Restarts", cell: (r) => s(r.restarts), className: "text-right" },
      { header: "Node", cell: (r) => s(r.node) },
      age,
    ],
  },
  deployments: {
    kind: "deployments", title: "Deployments", singular: "Deployment", eyebrow: "Workloads",
    description: "Stateless applications: scale, restart, edit, roll out and roll back.",
    columns: [
      { header: "Ready", cell: (r) => s(r.ready) },
      { header: "Up-to-date", cell: (r) => s(r.up_to_date), className: "text-right" },
      { header: "Available", cell: (r) => s(r.available), className: "text-right" },
      { header: "Images", cell: (r) => list(r.images) },
      { header: "Rollout", cell: (r) => <RolloutBadge rollout={r.rollout as Rollout} /> },
      age,
    ],
  },
  replicasets: {
    kind: "replicasets", title: "ReplicaSets", singular: "ReplicaSet", eyebrow: "Workloads",
    description: "Pod replica controllers (usually managed by Deployments).",
    columns: [
      { header: "Desired", cell: (r) => s(r.desired), className: "text-right" },
      { header: "Current", cell: (r) => s(r.current), className: "text-right" },
      { header: "Ready", cell: (r) => s(r.ready), className: "text-right" },
      { header: "Owner", cell: (r) => mono(r.owner) },
      { header: "Revision", cell: (r) => s(r.revision) },
      age,
    ],
  },
  statefulsets: {
    kind: "statefulsets", title: "StatefulSets", singular: "StatefulSet", eyebrow: "Workloads",
    description: "Stateful applications with stable identities and storage.",
    columns: [
      { header: "Ready", cell: (r) => s(r.ready) },
      { header: "Service", cell: (r) => mono(r.service) },
      { header: "Images", cell: (r) => list(r.images) },
      { header: "Rollout", cell: (r) => <RolloutBadge rollout={r.rollout as Rollout} /> },
      age,
    ],
  },
  daemonsets: {
    kind: "daemonsets", title: "DaemonSets", singular: "DaemonSet", eyebrow: "Workloads",
    description: "Pods that run on every (selected) node.",
    columns: [
      { header: "Desired", cell: (r) => s(r.desired), className: "text-right" },
      { header: "Ready", cell: (r) => s(r.ready), className: "text-right" },
      { header: "Up-to-date", cell: (r) => s(r.up_to_date), className: "text-right" },
      { header: "Images", cell: (r) => list(r.images) },
      { header: "Rollout", cell: (r) => <RolloutBadge rollout={r.rollout as Rollout} /> },
      age,
    ],
  },
  jobs: {
    kind: "jobs", title: "Jobs", singular: "Job", eyebrow: "Workloads",
    description: "Run-to-completion workloads.",
    columns: [
      { header: "Status", cell: (r) => <StatusPill tone={r.status === "Complete" ? "success" : r.status === "Failed" ? "danger" : "info"}>{s(r.status)}</StatusPill> },
      { header: "Completions", cell: (r) => s(r.completions) },
      { header: "Owner", cell: (r) => mono(r.owner) },
      age,
    ],
  },
  cronjobs: {
    kind: "cronjobs", title: "CronJobs", singular: "CronJob", eyebrow: "Workloads",
    description: "Scheduled jobs: run now, suspend or resume.",
    columns: [
      { header: "Schedule", cell: (r) => mono(r.schedule) },
      { header: "Suspended", cell: (r) => (r.suspend ? <StatusPill tone="warning">suspended</StatusPill> : "no") },
      { header: "Active", cell: (r) => s(r.active), className: "text-right" },
      { header: "Last run", cell: (r) => formatAge(r.last_schedule as string | null) },
      age,
    ],
  },
  services: {
    kind: "services", title: "Services", singular: "Service", eyebrow: "Networking",
    description: "ClusterIP, NodePort and LoadBalancer services.",
    columns: [
      { header: "Type", cell: (r) => s(r.type) },
      { header: "Cluster IP", cell: (r) => mono(r.cluster_ip) },
      { header: "External", cell: (r) => list(r.external) },
      { header: "Ports", cell: (r) => list(r.ports) },
      age,
    ],
  },
  ingresses: {
    kind: "ingresses", title: "Ingress", singular: "Ingress", eyebrow: "Networking",
    description: "HTTP and HTTPS routes from cluster entry points to Services. An ingress controller must be installed to serve routes.",
    columns: [
      { header: "Class", cell: (r) => s(r.ingress_class) },
      { header: "Hosts", cell: (r) => list(r.hosts) },
      { header: "Paths", cell: (r) => s(r.paths), className: "text-right" },
      { header: "Addresses", cell: (r) => list(r.addresses) },
      age,
    ],
  },
  networkpolicies: {
    kind: "networkpolicies", title: "Network policies", singular: "NetworkPolicy", eyebrow: "Networking",
    description: "Namespace scoped ingress and egress rules for selected pods. Enforcement depends on the cluster CNI.",
    columns: [
      { header: "Pod selector", cell: (r) => list(Object.entries((r.pod_selector ?? {}) as Record<string, string>).map(([k, v]) => `${k}=${v}`)) },
      { header: "Policy types", cell: (r) => list(r.policy_types) },
      { header: "Ingress rules", cell: (r) => s(r.ingress_rules), className: "text-right" },
      { header: "Egress rules", cell: (r) => s(r.egress_rules), className: "text-right" },
      age,
    ],
  },
  endpointslices: {
    kind: "endpointslices", title: "EndpointSlices", singular: "EndpointSlice", eyebrow: "Networking",
    description: "Backend addresses published for Services and consumed by kube-proxy and DNS-aware clients.",
    columns: [
      { header: "Service", cell: (r) => mono(r.service) },
      { header: "Address type", cell: (r) => s(r.address_type) },
      { header: "Ready", cell: (r) => `${s(r.ready_endpoints)}/${s(r.endpoints)}`, className: "text-right" },
      { header: "Addresses", cell: (r) => list(r.addresses) },
      { header: "Ports", cell: (r) => list(r.ports) },
      age,
    ],
  },
  persistentvolumes: {
    kind: "persistentvolumes", title: "Persistent volumes", singular: "PersistentVolume", eyebrow: "Storage", scope: "cluster",
    description: "Cluster-scoped storage capacity and its claim, class, phase and reclaim policy.",
    columns: [
      { header: "Phase", cell: (r) => s(r.phase) },
      { header: "Capacity", cell: (r) => s(r.capacity) },
      { header: "Access modes", cell: (r) => list(r.access_modes) },
      { header: "Storage class", cell: (r) => s(r.storage_class) },
      { header: "Claim", cell: (r) => mono(r.claim) },
      { header: "Reclaim", cell: (r) => s(r.reclaim_policy) },
      age,
    ],
  },
  persistentvolumeclaims: {
    kind: "persistentvolumeclaims", title: "Volume claims", singular: "PersistentVolumeClaim", eyebrow: "Storage",
    description: "Namespace-scoped requests for persistent storage. A matching StorageClass and provisioner may be required.",
    columns: [
      { header: "Phase", cell: (r) => s(r.phase) },
      { header: "Requested", cell: (r) => s(r.requested) },
      { header: "Bound volume", cell: (r) => mono(r.volume) },
      { header: "Storage class", cell: (r) => s(r.storage_class) },
      { header: "Access modes", cell: (r) => list(r.access_modes) },
      age,
    ],
  },
  storageclasses: {
    kind: "storageclasses", title: "Storage classes", singular: "StorageClass", eyebrow: "Storage", scope: "cluster",
    description: "Provisioning policy definitions. The referenced CSI or external provisioner must already be installed.",
    columns: [
      { header: "Default", cell: (r) => (r.default ? "Yes" : "No") },
      { header: "Provisioner", cell: (r) => mono(r.provisioner) },
      { header: "Binding", cell: (r) => s(r.binding_mode) },
      { header: "Reclaim", cell: (r) => s(r.reclaim_policy) },
      { header: "Expansion", cell: (r) => (r.allow_expansion ? "Allowed" : "No") },
      age,
    ],
  },
  csidrivers: {
    kind: "csidrivers", title: "CSI drivers", singular: "CSIDriver", eyebrow: "Storage", scope: "cluster",
    description: "Registered CSI driver capabilities. Driver controllers and node plugins are installed separately.",
    columns: [
      { header: "Attach required", cell: (r) => s(r.attach_required) },
      { header: "Pod info on mount", cell: (r) => s(r.pod_info_on_mount) },
      { header: "FS group policy", cell: (r) => s(r.fs_group_policy) },
      { header: "Volume lifecycle", cell: (r) => list(r.volume_lifecycle_modes) },
      age,
    ],
  },
  volumeattachments: {
    kind: "volumeattachments", title: "Volume attachments", singular: "VolumeAttachment", eyebrow: "Storage", scope: "cluster",
    description: "Controller-managed attachment state for a volume and node. These objects are read-only here.",
    columns: [
      { header: "Attached", cell: (r) => (r.attached ? "Yes" : "No") },
      { header: "Volume", cell: (r) => mono(r.persistent_volume) },
      { header: "Node", cell: (r) => mono(r.node) },
      { header: "Driver", cell: (r) => mono(r.driver) },
      age,
    ],
  },
  gatewayclasses: {
    kind: "gatewayclasses", title: "GatewayClasses", singular: "GatewayClass", eyebrow: "Networking", scope: "cluster",
    description: "Cluster-wide Gateway API controller classes. The Gateway API CRDs and a compatible controller are required.",
    columns: [
      { header: "Controller", cell: (r) => mono(r.controller) },
      { header: "Accepted", cell: (r) => s(r.accepted) },
      { header: "Reason", cell: (r) => s(r.reason) },
      age,
    ],
  },
  gateways: {
    kind: "gateways", title: "Gateways", singular: "Gateway", eyebrow: "Networking",
    description: "Traffic entry points and listeners managed by a Gateway API controller.",
    columns: [
      { header: "Class", cell: (r) => s(r.gateway_class) },
      { header: "Listeners", cell: (r) => s(r.listeners) },
      { header: "Programmed", cell: (r) => s(r.programmed) },
      { header: "Addresses", cell: (r) => list(r.addresses) },
      age,
    ],
  },
  httproutes: {
    kind: "httproutes", title: "HTTPRoutes", singular: "HTTPRoute", eyebrow: "Networking",
    description: "HTTP host and path routing rules attached to Gateways.",
    columns: [
      { header: "Hosts", cell: (r) => list(r.hosts) },
      { header: "Parents", cell: (r) => list(r.parents) },
      { header: "Rules", cell: (r) => s(r.rules), className: "text-right" },
      { header: "Accepted", cell: (r) => s(r.accepted) },
      { header: "Refs resolved", cell: (r) => s(r.resolved_refs) },
      age,
    ],
  },
  referencegrants: {
    kind: "referencegrants", title: "ReferenceGrants", singular: "ReferenceGrant", eyebrow: "Networking",
    description: "Cross-namespace references authorized for Gateway API routes.",
    columns: [
      { header: "From rules", cell: (r) => s(r.from_count), className: "text-right" },
      { header: "To rules", cell: (r) => s(r.to_count), className: "text-right" },
      age,
    ],
  },
  configmaps: {
    kind: "configmaps", title: "ConfigMaps", singular: "ConfigMap", eyebrow: "Configuration",
    description: "Non-secret configuration data.",
    columns: [{ header: "Keys", cell: (r) => list(r.keys) }, age],
  },
  secrets: {
    kind: "secrets", title: "Secrets", singular: "Secret", eyebrow: "Configuration",
    description: "Secret data. Values stay hidden until you reveal them; every reveal is audited.",
    columns: [{ header: "Type", cell: (r) => mono(r.type) }, { header: "Keys", cell: (r) => list(r.keys) }, age],
  },
  resourcequotas: {
    kind: "resourcequotas", title: "Resource quotas", singular: "ResourceQuota", eyebrow: "Configuration",
    description: "Namespace limits for compute, storage and object counts, with current usage.",
    columns: [
      { header: "Resources limited", cell: (r) => s(r.resource_count), className: "text-right" },
      { header: "Hard limits", cell: (r) => list(r.hard) },
      { header: "Used", cell: (r) => list(r.used) },
      age,
    ],
  },
  limitranges: {
    kind: "limitranges", title: "Limit ranges", singular: "LimitRange", eyebrow: "Configuration",
    description: "Default and maximum resource requests and limits within a namespace.",
    columns: [
      { header: "Limit types", cell: (r) => list(r.limit_types) },
      { header: "Rules", cell: (r) => s(r.limit_count), className: "text-right" },
      age,
    ],
  },
};
