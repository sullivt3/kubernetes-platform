// Pure manifest generation and validation for the new-deployment form (unit tested).

import { stringify } from "yaml";

const DNS = /^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$/;
export const QTY = /^$|^[0-9]+(\.[0-9]+)?(m|Ki|Mi|Gi|Ti|k|M|G|T)?$/;

export interface KV { key: string; value: string }
export interface VolumeRow { name: string; type: "emptyDir" | "configMap" | "secret" | "persistentVolumeClaim"; source: string; mountPath: string }
export type Probe = "none" | "http" | "tcp";

export interface DeploymentInput {
  name: string;
  namespace: string;
  image: string;
  replicas: number;
  ports: number[];
  env: KV[];
  requests: { cpu: string; memory: string };
  limits: { cpu: string; memory: string };
  readiness: { type: Probe; path: string; port: number };
  liveness: { type: Probe; path: string; port: number };
  volumes: VolumeRow[];
  pullSecrets: string;
  nodeSelector: KV[];
  spreadAcrossNodes: boolean;
  service: "none" | "ClusterIP" | "NodePort" | "LoadBalancer";
}

function probe(p: { type: Probe; path: string; port: number }) {
  if (p.type === "http") return { httpGet: { path: p.path || "/", port: p.port }, periodSeconds: 10 };
  if (p.type === "tcp") return { tcpSocket: { port: p.port }, periodSeconds: 10 };
  return undefined;
}

function compact<T extends Record<string, unknown>>(obj: T): T {
  return Object.fromEntries(Object.entries(obj).filter(([, v]) => v !== undefined && v !== "" && !(typeof v === "object" && v !== null && !Array.isArray(v) && Object.keys(v).length === 0) && !(Array.isArray(v) && v.length === 0))) as T;
}

/** Build Deployment (+ optional Service) manifests from the form. Exported for tests. */
export function buildManifest(f: DeploymentInput): string {
  const labels = { app: f.name };
  const container = compact({
    name: f.name,
    image: f.image,
    ports: f.ports.map((p) => ({ containerPort: p })),
    env: f.env.filter((e) => e.key).map((e) => ({ name: e.key, value: e.value })),
    resources: compact({
      requests: compact({ cpu: f.requests.cpu || undefined, memory: f.requests.memory || undefined }),
      limits: compact({ cpu: f.limits.cpu || undefined, memory: f.limits.memory || undefined }),
    }),
    readinessProbe: probe(f.readiness),
    livenessProbe: probe(f.liveness),
    volumeMounts: f.volumes.filter((v) => v.name && v.mountPath).map((v) => ({ name: v.name, mountPath: v.mountPath })),
  });
  const volumes = f.volumes.filter((v) => v.name && v.mountPath).map((v) => {
    if (v.type === "emptyDir") return { name: v.name, emptyDir: {} };
    if (v.type === "configMap") return { name: v.name, configMap: { name: v.source } };
    if (v.type === "secret") return { name: v.name, secret: { secretName: v.source } };
    return { name: v.name, persistentVolumeClaim: { claimName: v.source } };
  });
  const podSpec = compact({
    containers: [container],
    volumes,
    imagePullSecrets: f.pullSecrets.split(",").map((s) => s.trim()).filter(Boolean).map((name) => ({ name })),
    nodeSelector: Object.fromEntries(f.nodeSelector.filter((n) => n.key).map((n) => [n.key, n.value])),
    topologySpreadConstraints: f.spreadAcrossNodes
      ? [{ maxSkew: 1, topologyKey: "kubernetes.io/hostname", whenUnsatisfiable: "ScheduleAnyway", labelSelector: { matchLabels: labels } }]
      : undefined,
  });
  const docs: unknown[] = [
    {
      apiVersion: "apps/v1",
      kind: "Deployment",
      metadata: { name: f.name, namespace: f.namespace, labels },
      spec: { replicas: f.replicas, selector: { matchLabels: labels }, template: { metadata: { labels }, spec: podSpec } },
    },
  ];
  if (f.service !== "none" && f.ports.length) {
    docs.push({
      apiVersion: "v1",
      kind: "Service",
      metadata: { name: f.name, namespace: f.namespace, labels },
      spec: { type: f.service, selector: labels, ports: f.ports.map((p) => ({ name: `p${p}`, port: p, targetPort: p })) },
    });
  }
  return docs.map((d) => stringify(d)).join("---\n");
}

export function validateDeployment(f: DeploymentInput): string[] {
  const errors: string[] = [];
  if (!DNS.test(f.name)) errors.push("Name: lowercase letters, digits and '-'");
  if (!DNS.test(f.namespace)) errors.push("Namespace is required");
  if (!f.image.trim() || /\s/.test(f.image)) errors.push("Container image is required (no spaces)");
  if (!Number.isInteger(f.replicas) || f.replicas < 0 || f.replicas > 1000) errors.push("Replicas must be 0–1000");
  if (f.ports.some((p) => !Number.isInteger(p) || p < 1 || p > 65535)) errors.push("Ports must be 1–65535");
  for (const q of [f.requests.cpu, f.requests.memory, f.limits.cpu, f.limits.memory]) if (!QTY.test(q)) errors.push(`Invalid quantity "${q}" (e.g. 250m, 512Mi)`);
  if (f.env.some((e) => e.key && !/^[A-Za-z_][A-Za-z0-9_.-]*$/.test(e.key))) errors.push("Environment variable names must start with a letter or _");
  if (f.volumes.some((v) => v.name && (!DNS.test(v.name) || !v.mountPath.startsWith("/") || (v.type !== "emptyDir" && !v.source)))) errors.push("Each volume needs a valid name, an absolute mount path and a source");
  if (f.service !== "none" && !f.ports.length) errors.push("A Service needs at least one container port");
  return errors;
}

