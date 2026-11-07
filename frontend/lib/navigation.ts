import {
  Activity,
  Boxes,
  type LucideIcon,
  HardDrive,
  LayoutDashboard,
  Network,
  Server,
  Settings2,
  ShieldCheck,
  SlidersHorizontal,
  Workflow,
} from "lucide-react";

/** Roadmap phases, used to label features that are not built yet. */
export const PHASES: Record<number, string> = {
  1: "Dashboard & existing cluster connectivity",
  2: "Kubernetes workload management",
  3: "Infrastructure integrations",
  4: "Kubernetes installation engine",
  5: "Full cluster lifecycle management",
  6: "Networking & storage",
  7: "Enterprise administration",
  8: "Advanced operations",
};

export interface NavItem {
  title: string;
  href: string;
  description: string;
  /** Undefined = implemented. A number = planned for that roadmap phase. */
  phase?: number;
  keywords?: string[];
}

export interface NavSection {
  title: string;
  icon: LucideIcon;
  items: NavItem[];
}

export const NAVIGATION: NavSection[] = [
  {
    title: "Overview",
    icon: LayoutDashboard,
    items: [
      {
        title: "Dashboard",
        href: "/",
        description: "Platform health, active operations and recent activity.",
        keywords: ["home", "health"],
      },
    ],
  },
  {
    title: "Infrastructure",
    icon: Server,
    items: [
      {
        title: "Machines",
        href: "/infrastructure/machines",
        description: "Linux servers registered with the platform: SSH access, host keys, OS, hardware and Kubernetes prerequisites.",
        keywords: ["inventory", "servers", "physical", "hardware", "ssh", "nodes"],
      },
      { title: "Provisioning", href: "/infrastructure/provisioning", description: "Create machines through a provider: local Docker nodes or Proxmox VMs.", keywords: ["create", "vm", "docker", "local"] },
      { title: "Virtual machines", href: "/infrastructure/virtual-machines", description: "Machines on Proxmox and local providers, discovered from each provider.", keywords: ["proxmox", "docker", "import"] },
      { title: "Cloud resources", href: "/infrastructure/cloud-resources", description: "AWS, Azure and Google Cloud instance inventory (read-only).", keywords: ["aws", "ec2", "azure", "gcp", "inventory"] },
      { title: "Network discovery", href: "/infrastructure/discovery", description: "Scan address ranges for SSH servers to register.", keywords: ["scan", "find", "ssh"] },
      { title: "Providers", href: "/infrastructure/providers", description: "Provider connections, supported providers, their capabilities and configuration.", keywords: ["proxmox", "aws", "azure", "gcp", "ssh", "connections"] },
    ],
  },
  {
    title: "Kubernetes",
    icon: Boxes,
    items: [
      { title: "Clusters", href: "/kubernetes/clusters", description: "Clusters created or registered with the platform.", keywords: ["kubeadm"] },
      { title: "Create cluster", href: "/kubernetes/create", description: "Guided wizard that installs Kubernetes on registered machines.", keywords: ["install", "new", "wizard"] },
      { title: "Import cluster", href: "/kubernetes/import", description: "Connect an existing cluster using a kubeconfig.", keywords: ["kubeconfig", "connect", "existing"] },
      { title: "Apply YAML", href: "/kubernetes/apply", description: "Validate, diff and apply manifests (server-side dry run).", keywords: ["yaml", "manifest", "kubectl apply", "editor"] },
      { title: "Distributions", href: "/kubernetes/distributions", description: "Supported Kubernetes distributions and infrastructure compatibility.", keywords: ["kubeadm", "k3s", "rke2", "kind", "eks", "aks", "gke"] },
      { title: "Cluster settings", href: "/kubernetes/settings", description: "Connection settings and lifecycle actions for the selected cluster.", keywords: ["connection", "unregister"] },
      { title: "Upgrades", href: "/kubernetes/upgrades", description: "Plan and execute Kubernetes version upgrades.", phase: 5 },
      { title: "Nodes", href: "/kubernetes/nodes", description: "Live node status, capacity, utilization, conditions and events." },
      { title: "Namespaces", href: "/kubernetes/namespaces", description: "Namespaces and the health of their pods." },
      { title: "Topology", href: "/kubernetes/topology", description: "Where pods run, by node." },
    ],
  },
  {
    title: "Workloads",
    icon: Workflow,
    items: [
      { title: "Pods", href: "/workloads/pods", description: "Pods: status, logs, events, shell access and YAML." },
      { title: "Deployments", href: "/workloads/deployments", description: "Create, scale, roll out and roll back deployments." },
      { title: "ReplicaSets", href: "/workloads/replicasets", description: "ReplicaSets and their owners." },
      { title: "StatefulSets", href: "/workloads/statefulsets", description: "Stateful workloads with stable identity and storage." },
      { title: "DaemonSets", href: "/workloads/daemonsets", description: "Per-node workloads." },
      { title: "Jobs", href: "/workloads/jobs", description: "Run-to-completion workloads." },
      { title: "CronJobs", href: "/workloads/cronjobs", description: "Scheduled jobs." },
      { title: "Application deployments", href: "/workloads/applications", description: "Deploy applications from images or Helm charts.", phase: 8 },
    ],
  },
  {
    title: "Networking",
    icon: Network,
    items: [
      { title: "Services", href: "/networking/services", description: "ClusterIP, NodePort and LoadBalancer services.", keywords: ["clusterip", "nodeport", "loadbalancer"] },
      { title: "Ingress", href: "/networking/ingress", description: "HTTP routing into the cluster.", phase: 6 },
      { title: "Gateway API", href: "/networking/gateway-api", description: "Gateways and routes.", phase: 6 },
      { title: "Network policies", href: "/networking/network-policies", description: "Pod-level traffic rules.", phase: 6 },
      { title: "DNS & service discovery", href: "/networking/dns", description: "Cluster DNS configuration and service discovery.", phase: 6 },
    ],
  },
  {
    title: "Storage",
    icon: HardDrive,
    items: [
      { title: "Persistent volumes", href: "/storage/persistent-volumes", description: "Cluster-wide storage volumes.", phase: 6 },
      { title: "Volume claims", href: "/storage/persistent-volume-claims", description: "Persistent volume claims by namespace.", phase: 6 },
      { title: "Storage classes", href: "/storage/storage-classes", description: "Dynamic provisioning classes.", phase: 6 },
      { title: "Volume attachments", href: "/storage/volume-attachments", description: "Volumes attached to nodes.", phase: 6 },
      { title: "CSI drivers", href: "/storage/csi-drivers", description: "Installed Container Storage Interface drivers.", phase: 6 },
    ],
  },
  {
    title: "Configuration",
    icon: SlidersHorizontal,
    items: [
      { title: "ConfigMaps", href: "/configuration/configmaps", description: "Non-secret configuration data." },
      { title: "Secrets", href: "/configuration/secrets", description: "Kubernetes Secrets (values revealed only on explicit request)." },
      { title: "Resource quotas", href: "/configuration/resource-quotas", description: "Namespace resource limits.", phase: 6 },
      { title: "Limit ranges", href: "/configuration/limit-ranges", description: "Default and maximum container resources.", phase: 6 },
      { title: "Custom resources", href: "/configuration/custom-resources", description: "CRDs and dynamic resource exploration.", phase: 8, keywords: ["crd"] },
    ],
  },
  {
    title: "Security",
    icon: ShieldCheck,
    items: [
      { title: "Credentials", href: "/security/credentials", description: "Encrypted SSH keys, kubeconfigs and cloud credentials.", keywords: ["ssh", "kubeconfig", "secret", "key"] },
      { title: "Roles & bindings", href: "/security/rbac", description: "Kubernetes Roles, ClusterRoles and their bindings.", phase: 7, keywords: ["rbac"] },
      { title: "Service accounts", href: "/security/service-accounts", description: "Workload identities.", phase: 7 },
      { title: "Security policies", href: "/security/policies", description: "Pod security standards and admission policies.", phase: 7 },
      { title: "Security auditing", href: "/security/auditing", description: "Security posture reports and findings.", phase: 7 },
    ],
  },
  {
    title: "Operations",
    icon: Activity,
    items: [
      { title: "Operations", href: "/operations", description: "Active and historical operations with live execution logs.", keywords: ["jobs", "history", "tasks"] },
      { title: "Monitoring", href: "/ops/monitoring", description: "Metrics from Prometheus and Grafana.", phase: 8 },
      { title: "Logs", href: "/ops/logs", description: "Application logs (Loki).", phase: 8 },
      { title: "Events", href: "/ops/events", description: "Recent Kubernetes events for the selected cluster." },
      { title: "Alerts", href: "/ops/alerts", description: "Alert rules and active alerts.", phase: 8 },
      { title: "Backup & restore", href: "/ops/backup", description: "Cluster and platform backups.", phase: 7 },
      { title: "Command center", href: "/ops/command-center", description: "Embedded terminal with kubectl and Helm.", phase: 8, keywords: ["terminal", "kubectl", "helm", "yaml"] },
    ],
  },
  {
    title: "Administration",
    icon: Settings2,
    items: [
      { title: "Users", href: "/administration/users", description: "Platform users and identity provider integration.", phase: 7 },
      { title: "Organizations & projects", href: "/administration/organizations", description: "Tenant isolation boundaries.", phase: 7 },
      { title: "Permissions", href: "/administration/permissions", description: "Application role-based access control.", phase: 7 },
      { title: "Audit log", href: "/administration/audit-log", description: "Every change made through the platform.", keywords: ["audit", "history"] },
      { title: "Integrations", href: "/administration/integrations", description: "Identity, secrets, monitoring and Git integrations.", phase: 8 },
      { title: "Settings", href: "/administration/settings", description: "Platform configuration and environment.", keywords: ["version", "config"] },
    ],
  },
];

export const ALL_NAV_ITEMS: NavItem[] = NAVIGATION.flatMap((section) => section.items);

export function findNavItem(href: string): NavItem | undefined {
  return ALL_NAV_ITEMS.find((item) => item.href === href);
}

export function findNavSection(href: string): NavSection | undefined {
  return NAVIGATION.find((section) => section.items.some((item) => item.href === href));
}

/** Items not implemented yet; each is served by the shared "planned feature" page. */
export const PLANNED_ITEMS: NavItem[] = ALL_NAV_ITEMS.filter((item) => item.phase !== undefined);

/** Active nav item for a pathname: exact match, else the longest prefix match. */
export function activeHref(pathname: string): string | undefined {
  if (findNavItem(pathname)) return pathname;
  return ALL_NAV_ITEMS.filter((i) => i.href !== "/" && pathname.startsWith(`${i.href}/`)).sort(
    (a, b) => b.href.length - a.href.length,
  )[0]?.href;
}
