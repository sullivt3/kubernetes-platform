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
};
