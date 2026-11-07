// TypeScript mirrors of the backend API schemas (backend/app/api/routes/*).
// Keep in sync with the OpenAPI document at /api/v1/openapi.json.

export type HealthStatus = "ok" | "degraded" | "unavailable";

export interface ComponentHealth {
  name: string;
  status: HealthStatus;
  message: string;
  latency_ms: number | null;
  details: Record<string, unknown>;
}

export interface ReadinessReport {
  status: HealthStatus;
  checked_at: string;
  components: ComponentHealth[];
}

export interface SystemInfo {
  app_name: string;
  version: string;
  environment: string;
  auth_mode: string;
  orchestration_mode: string;
  secret_backend: string;
  secret_primary_key_id: string | null;
  current_user: { subject: string; display_name: string; roles: string[] };
}

export type ImplementationStatus = "available" | "preview" | "planned";

export interface JsonSchema {
  type?: string;
  title?: string;
  description?: string;
  properties?: Record<string, JsonSchema & { default?: unknown }>;
  required?: string[];
  [key: string]: unknown;
}

export interface ProviderDescriptor {
  type: string;
  display_name: string;
  description: string;
  category: string;
  implementation_status: ImplementationStatus;
  planned_phase: number | null;
  capabilities: string[];
  credential_kinds: string[];
  credential_required: boolean;
  config_schema: JsonSchema;
}

export interface DistributionDescriptor {
  type: string;
  display_name: string;
  description: string;
  kind: "self_managed" | "managed" | "local";
  implementation_status: ImplementationStatus;
  planned_phase: number | null;
  capabilities: string[];
  supported_providers: string[];
  supported_runtimes: string[];
  supported_cnis: string[];
  default_cni: string;
  supports_existing_machines: boolean;
  options_schema: JsonSchema;
}

export type OperationStatus =
  | "pending"
  | "queued"
  | "running"
  | "cancelling"
  | "succeeded"
  | "failed"
  | "cancelled";

export type StepStatus = "pending" | "running" | "succeeded" | "failed" | "skipped" | "cancelled";

export interface OperationError {
  code: string;
  message: string;
  step?: string;
  retryable?: boolean;
}

export interface OperationSummary {
  id: string;
  type: string;
  title: string;
  status: OperationStatus;
  target_type: string | null;
  target_id: string | null;
  target_name: string | null;
  progress: number;
  current_step: string | null;
  attempt: number;
  recovery_count: number;
  requested_by_subject: string;
  error: OperationError | null;
  created_at: string;
  queued_at: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface OperationStep {
  key: string;
  name: string;
  position: number;
  status: StepStatus;
  attempts: number;
  started_at: string | null;
  finished_at: string | null;
  error: OperationError | null;
  output: Record<string, unknown>;
}

export interface OperationDetail extends OperationSummary {
  params: Record<string, unknown>;
  result: Record<string, unknown> | null;
  worker_id: string | null;
  heartbeat_at: string | null;
  cancel_requested_at: string | null;
  parent_operation_id: string | null;
  steps: OperationStep[];
}

export interface OperationLog {
  id: number;
  ts: string;
  level: "debug" | "info" | "warning" | "error";
  step_key: string | null;
  message: string;
}

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export const CREDENTIAL_KINDS = [
  "ssh_private_key",
  "ssh_password",
  "kubeconfig",
  "proxmox_api_token",
  "vsphere_password",
  "aws_access_key",
  "azure_service_principal",
  "gcp_service_account",
  "generic_token",
] as const;
export type CredentialKind = (typeof CREDENTIAL_KINDS)[number];

export interface Credential {
  id: string;
  name: string;
  kind: CredentialKind;
  description: string | null;
  public_metadata: Record<string, string>;
  secret_backend: string;
  key_id: string;
  project_id: string | null;
  created_at: string;
  updated_at: string;
  rotated_at: string | null;
  last_used_at: string | null;
}

export interface CredentialDetail extends Credential {
  used_by: string[];
}

export interface CredentialCreate {
  name: string;
  kind: CredentialKind;
  secret_value: string;
  description?: string | null;
  public_metadata?: Record<string, string>;
}

export interface AuditEvent {
  id: number;
  occurred_at: string;
  actor_id: string | null;
  actor_subject: string;
  action: string;
  resource_type: string;
  resource_id: string | null;
  resource_name: string | null;
  outcome: "success" | "failure" | "denied";
  request_id: string | null;
  client_ip: string | null;
  details: Record<string, unknown>;
}

export interface ClusterSummary {
  id: string;
  name: string;
  display_name: string | null;
  description: string | null;
  distribution: string;
  origin: string;
  lifecycle_state: string;
  kubernetes_version: string | null;
  api_endpoint: string | null;
  last_contact_at: string | null;
  created_at: string;
  node_count: number;
  active_operation_id: string | null;
}

export interface ClusterNode {
  machine_id: string;
  machine_name: string;
  node_name: string | null;
  role: "control_plane" | "worker";
  membership_state: string;
  address: string | null;
  os: string | null;
  cpu_cores: number | null;
  memory_mb: number | null;
}

export interface ClusterDetail extends ClusterSummary {
  spec: ClusterSpecInput;
  status_detail: { error?: OperationError; operation_id?: string; cni?: Record<string, string> };
  has_kubeconfig: boolean;
  nodes: ClusterNode[];
  operations: OperationSummary[];
}

export interface Taint {
  key: string;
  value?: string | null;
  effect: "NoSchedule" | "PreferNoSchedule" | "NoExecute";
}

export interface NodePoolInput {
  name: string;
  count: number;
  existing_machine_ids: string[];
  labels?: Record<string, string>;
  taints?: Taint[];
}

export interface ClusterSpecInput {
  name: string;
  distribution: string;
  provider_type: string;
  kubernetes_version: string;
  container_runtime?: string;
  network: { pod_cidr: string; service_cidr: string; dns_domain: string; cni: string };
  control_plane: NodePoolInput & { endpoint?: string | null; load_balancer?: string };
  worker_pools: NodePoolInput[];
  options: Record<string, unknown>;
}

export interface ClusterValidation {
  valid: boolean;
  problems: string[];
  steps: { key: string; name: string }[];
  nodes: { machine_id: string; name: string; role: string; pool: string }[];
}

export interface KubernetesVersions {
  available: boolean;
  latest: string | null;
  versions: { minor: string; latest_patch: string }[];
  minimum_minor: string;
  message: string | null;
}

export interface MachineCheck {
  key: string;
  label: string;
  status: "pass" | "warn" | "fail";
  message: string;
}

export type MachineState =
  | "requested"
  | "provisioning"
  | "ready"
  | "unreachable"
  | "error"
  | "deprovisioning"
  | "deleted";

export interface Machine {
  id: string;
  name: string;
  lifecycle_state: MachineState;
  ssh_host: string | null;
  ssh_port: number;
  ssh_username: string | null;
  access_credential_id: string | null;
  host_key_fingerprint: string | null;
  addresses: { type: string; address: string }[];
  os_info: {
    pretty_name?: string;
    kernel?: string;
    hostname?: string;
    kubernetes?: { kubeadm?: string | null; containerd?: string | null; member_of_cluster?: boolean };
  };
  hardware: {
    cpu_cores?: number;
    memory_mb?: number;
    architecture?: string;
    root_disk_free_gb?: number;
    swap_mb?: number;
  };
  checks: MachineCheck[];
  labels: Record<string, string>;
  last_error: string | null;
  inspected_at: string | null;
  created_at: string;
  cluster_name: string | null;
  cluster_role: string | null;
}

export interface MachineRegister {
  name: string;
  host: string;
  port?: number | null;
  username?: string | null;
  credential_id: string;
}

export interface ApiErrorBody {
  error: {
    code: string;
    message: string;
    details: Record<string, unknown>;
    request_id: string | null;
  };
}
