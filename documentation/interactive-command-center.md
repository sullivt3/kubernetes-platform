# Interactive Command Center

Operations → Command center now offers disposable interactive sessions for
**platform administrators**, plus namespace-scoped read-only sessions for explicitly
assigned operators. Both require `clusters.write` and `terminal.exec`. Full access
additionally requires `platform.manage`, `secrets.reveal`, an explicit full-access
acknowledgement and typing the selected cluster's name.

## Namespace-scoped operator access

Apply migrations through `0018`, then restart API/worker/scheduler. In Command center,
platform administrators open **Manage read-only namespace assignments**, choose an
existing platform user and application namespace, and assign access. The user
must already have operator permissions in the cluster's organization/project.
Assignment does not promote a viewer or grant access to another project.

Operators choose an **Assigned namespace**, acknowledge read-only access and type
the cluster name. Their shell's kubeconfig defaults to that namespace. Its bound
service-account token receives a dedicated Role/RoleBinding there; the runtime
stays in a separate disposable namespace. No ClusterRoleBinding is created.
Allowed reads are get/list/watch for Pods, Services, Events, PVCs, ResourceQuotas,
LimitRanges, Deployments, ReplicaSets, StatefulSets, DaemonSets, Jobs and CronJobs.
Secrets, ConfigMaps, pod logs, writes, RBAC, TokenRequests, exec/attach, port
forwarding, API proxies, nodes and other namespaces are excluded. Existing
cluster-wide grants to all authenticated identities can augment Kubernetes RBAC;
administrators must keep cluster authorization consistent with this profile.

Assignments pin namespace UID, user and cluster identity. Recreating a namespace
requires a new assignment. Creation, worker preparation, attachment and heartbeats
check current permissions/assignment. Revoking an assignment atomically marks its
sessions Closing and queues cleanup. Cleanup revokes subjects and removes only
owned RoleBindings/Roles and the runtime, retaining pending state on outages or
finalizers. Runtime grant names are protected from ordinary platform resource APIs.
The five-second socket checks and asynchronous cleanup are not instantaneous
revocation; token expiry remains an independent bound during outages.

This profile restricts the delegated shell credential. It does not replace the
platform's existing operator permissions in other management pages, or constrain
cluster administrators who can modify RBAC directly.

A full-access administrator shell has **cluster-admin privileges** and can read Secrets,
change workloads and create persistent resources or credentials. Commands bypass
individual reviews. This mode is for trusted platform administrators; a disposable
pod is not a hostile-code sandbox against someone who controls the cluster.
Ending a session removes its disposable namespace and grant. Changes and resources
created elsewhere are not rolled back.
Ordinary resource, Secret, manifest and pod-terminal paths reserve the session
namespace/grant names (`kp-shell-` followed by 32 lowercase hex characters); use
the dedicated session controls rather than those paths. They are omitted from
ordinary resource inventories, so inspect lifecycle audit for this temporary access.

## Enable the runtime

### Registry distribution

The manually dispatched **Publish Command Center runtime** GitHub Actions workflow
(`.github/workflows/command-runtime.yml`) builds Linux amd64 and arm64 images and
publishes `ghcr.io/<owner>/<repository>/command-runtime:sha-<commit>` using the
workflow's short-lived package token. It does not require a personal registry
password or publish on pull requests. Action dependencies are pinned to commits.
The run summary records the immutable multi-architecture manifest digest; use
that digest, rather than the convenience tag, in platform configuration.

Run it from the repository's **Actions** tab or with:

```sh
gh workflow run command-runtime.yml --ref main
```

A new GHCR package is private by default even for a public source repository.
For this public tools-only image, a package administrator can change its visibility
to **Public** in the package settings. Confirm an unauthenticated pull from each
node architecture before enabling it. Private images instead require node-side
registry authentication; platform GitHub login does not grant kubelet pull access.
The build context excludes local `.env` files and contains only the tools copied
by the runtime Dockerfile; cluster credentials are injected separately per session.

Verify the exact configured reference without relying on a local preload:

```sh
docker buildx imagetools inspect ghcr.io/<owner>/<repository>/command-runtime@sha256:<digest>
# On each node, with its normal registry trust/auth configuration:
sudo crictl pull ghcr.io/<owner>/<repository>/command-runtime@sha256:<digest>
```

Record the publishing commit, workflow run and verified digest in the deployment
handoff. A replacement node needs working HTTPS/DNS access to the registry and
the same trust/auth configuration. Registry availability is independent of the
management workstation and its Docker image cache.

### Manual build and configuration

Build the tools image using `backend/` as the build context:

```sh
docker build -f infrastructure/containers/command-runtime.Dockerfile \
  -t registry.example.com/platform/command-runtime:1 backend
```

Publish it to your trusted registry and configure the final **manifest digest**
on both API and worker/scheduler hosts:

```text
COMMAND_CENTER_RUNTIME_IMAGE=registry.example.com/platform/command-runtime@sha256:<64-hex-manifest-digest>
COMMAND_CENTER_KUBECTL_MINOR=37
```

Keep the feature disabled by leaving `COMMAND_CENTER_RUNTIME_IMAGE` unset until
that image is available on eligible cluster nodes. Unsetting it prevents new
sessions; end existing sessions separately. Private registry access requires
node-side authentication or preloading; this slice does not copy registry passwords
to the runtime. It uses `IfNotPresent` with the pinned digest. Apply Alembic migration
through `0018`, then restart API, worker and scheduler. A running Celery scheduler is needed
for unattended cleanup.

The supplied image contains checksum-verified **kubectl 1.37.2** and **Helm 4.3.2**,
plus an Alpine shell. `COMMAND_CENTER_KUBECTL_MINOR` must match the actual image's
kubectl version. Session creation rejects unknown cluster versions or a difference
of more than one minor version. Build a compatible trusted image before enabling
sessions for older/newer clusters. An image digest alone does not establish trust:
review/build the image before configuring it.

The management connection must be able to create namespaces, service accounts,
cluster-admin ClusterRoleBindings (administrator mode), namespaced Roles/RoleBindings,
NetworkPolicies, pods, Secrets, TokenRequests,
and pod exec connections. Granting a role requires the corresponding Kubernetes
bind/escalation authority; the platform does not bypass Kubernetes authorization.
A failed setup is cleaned up rather than falling back to long-lived credentials.

## Use a session

Acknowledge full access and select **Prepare interactive session**. Type the
cluster name in the confirmation dialog. Preparation appears as a tracked operation.
Once Ready, select **Open interactive terminal** within 60 seconds. The terminal
supports kubectl, installed-release Helm operations, shell input, resizing and
Ctrl+C. **End session** requests credential revocation and runtime removal.

Sessions are single-use. A disconnected session cannot be reattached; create a new
one after cleanup. Changing cluster or closing the terminal tab disconnects the
socket. An unclaimed runtime is removed after its attachment window. If navigation
occurs while preparation is pending, that window and the absolute lease still apply.
One non-closed session per user/cluster and at most four per cluster are
allowed. Cleanup-pending sessions count toward the limit.

By default, the terminal can reach only the cluster API under the installed NetworkPolicy.
It uses the API Service IP with verified `kubernetes.default.svc` TLS identity,
plus allow rules for the observed API endpoint addresses/ports. There is no DNS
or internet egress rule. Enforcement requires a NetworkPolicy-capable CNI; the
platform installs the policy but does not attest every CNI implementation.
Optional repository access is described below; Helm management remains available
without enabling terminal repository egress.
A trusted cluster administrator can change these policies using their cluster
privileges; they are not protection against malicious administrators.

Other cluster operations can run concurrently. Reviewed mutations still check
content drift, but interactive commands are not individually serialized with them.
Sessions must be ended and cleanup completed before cluster connection changes,
deletion or unregistration. Cleanup also binds the kube-system namespace UID so a
different cluster cannot be mistaken for successful removal.

## Credentials and isolation

Every session gets a unique namespace, service account, grant and pod. The runtime
uses a non-root UID, RuntimeDefault seccomp, no Linux capabilities, no privilege
escalation, a read-only root filesystem and bounded memory-backed home/tmp volumes.
It has no host mounts, host network or management-host filesystem access. Resource
limits are 500m CPU, 256 MiB memory and 64 MiB ephemeral storage; requests are 50m
CPU and 64 MiB memory.

The parent cluster kubeconfig's client key/token is never copied into the pod.
Instead, the backend requests a **600-second TokenRequest bound to the runtime
pod UID** and refuses a response whose expiry exceeds the requested bound. Only
that delegated token and cluster CA go into an ephemeral Secret-mounted kubeconfig.
The service account does not automount or refresh tokens. Credential provisioning
does not persist the delegated token in the platform database. Opt-in terminal
recordings can contain credentials a user prints; treat their output as sensitive.
See Kubernetes'
[service-account token documentation](https://kubernetes.io/docs/reference/access-authn-authz/service-accounts-admin/)
for TokenRequest and bound-token semantics.

Namespace/pod isolation does not limit an acknowledged cluster administrator's
Kubernetes permissions: the token intentionally grants cluster-admin. The initial
full-access profile remains platform-admin-only. The read-only operator profile
uses the separate namespace Role described above.

## Limits, revocation and recovery

- A session lease lasts at most 10 minutes from creation. The pod has its own
  600-second active deadline. Delegated tokens expire independently, within 600
  seconds of issuance; no refresh is performed by the platform.
- The WebSocket checks current login/membership, selected context, lease and cluster
  state every five seconds. Role/session/context changes close the connection.
- Input inactivity ends the connection after two minutes. Resizing does not reset
  that timer. Total input is limited to 1 MiB and output to 2 MiB, with bounded
  frames and terminal scrollback. These limits can end a long watch/log command.
- On close, a durable Closing state is written. Cleanup first empties the owned
  ClusterRoleBinding's subjects, then deletes the grant and namespace using UID
  preconditions. Finalizers are preserved. Closed is recorded only after both
  grant and namespace absence are observed.
- A database advisory lock serializes preparation and cleanup across replicas.
  Close can mark a preparing session immediately, while cleanup waits for the
  provisioner to exit. Preparation rechecks the original administrator before
  each sensitive phase, including immediately before publishing the credential.
- Celery beat requests cleanup every 10 seconds. Unattached, expired, stalled,
  closing or heartbeat-abandoned sessions are retried. A lost API process leaves a
  durable lease; a stale active heartbeat is eligible after 20 seconds.
- If the cluster/API/Redis is unavailable, cleanup can remain pending. The pod
  deadline and token expiry remain independent backstops. Once connectivity and
  workers return, cleanup retries without accepting replacement resources or
  stripping finalizers. A fully privileged administrator can deliberately create
  other credentials or grants; automatic cleanup does not undo that activity.

Session creation, readiness, attachment and closure are audited, with the session
ID linking to its requesting user and preparation operation. Without explicit
recording consent, terminal input/output is not retained. Kubernetes request
auditing is separate from optional output recordings and must be configured on
the cluster when needed.

## Controlled external repository access

Platform administrators use **Manage repository destinations** to configure up to
10 exact public DNS hostnames per cluster. Sessions stay API-only unless the user
selects **Enable HTTPS repository access** before preparation. The selected policy
is bound to the lease; saving a policy ends sessions that opted into repository
access, and preparation/attachment/heartbeats reject a changed policy.

Preparation resolves the approved names and rejects empty, oversized, mixed
public/private, metadata, loopback and mapped-address answers. The pod gets pinned
host aliases and NetworkPolicy rules for those public addresses on TCP 443 only.
General DNS and unrestricted internet access remain closed. Normal client TLS
verification remains enabled. Include every index, chart download, authentication,
OCI blob and redirect host needed by the repository; unlisted dependencies fail
rather than opening wider egress. Address changes require a new session.

This is an **IP-level HTTPS allowlist**, not a hostname or URL firewall. Other
virtual hosts on an approved shared/CDN IP can be reachable; do not use it as a
guarantee against that access or against data sent to an approved destination.
Loopback is the disposable pod itself, not the management host. Full cluster
administrators can change Kubernetes policies; read-only operator tokens cannot.
Enforcement requires a working NetworkPolicy-capable CNI. Private network
repositories, custom CA configuration and automatic copying of stored Helm/GitHub
credentials are excluded. User-supplied CLI credentials/files stay in the ephemeral
home; recording output can expose anything echoed or printed.

## Opt-in terminal transcripts

Recording is **off by default**. Select the explicit consent checkbox before
preparing a session. The UI shows recording ON/OFF while attached. Recording stores
terminal output, including commands/passwords echoed by the TTY; it does not hook
raw input events or provide a complete structured command audit. Redaction cannot
make arbitrary shell output safe. Session creation records the opt-in metadata.

Each output frame is encrypted with the platform SecretStore before delivery to
the browser. Frames have bounded ordering/count, a cumulative 2 MiB limit and a
24-hour retention deadline measured from session creation. At 4,096 frames or on
storage/encryption failure, the session ends rather than displaying unrecorded
output. Retain the relevant encryption keys while recordings or backups need them.

**Recorded terminal output** offers paginated metadata, an explicit audited JSON
download and deletion after the session is Closed. The author needs current
terminal/Secret-reveal permissions and cluster/project access. Platform
administrators can include other users' recordings within the selected scope.
Other operators/viewers cannot download those records. Decrypted output is fetched
only on download, outside shared query caches; responses use `no-store` and an
attachment filename. Active downloads may be partial, and abrupt API loss can
truncate a recording. The export includes the session status and termination reason.

Expired recordings become unavailable immediately, even during a scheduler outage.
The session reaper deletes expired encrypted chunks every ten seconds when
worker/scheduler/database access is available. Early deletion removes chunks and
prevents further export; lifecycle/export/delete audit metadata remains. Database
backups and downloaded copies have independent retention. Encrypted recordings are
included in database backups; backup access/retention must account for them.

## Validation

`backend/tests/test_command_sessions.py` exercises lease persistence, hardening,
bound TokenRequest payloads/expiry refusal, author/role boundaries, origin checks,
single attachment, revocation during preparation and active sockets, disconnect,
cleanup retries, namespace finalizers, replacement-resource refusal, and
close-versus-provision locking through the real database and operation engine.
Simulated transport coverage is distinct from a real browser/cluster exec drill.

Local validation on 2026-11-24 passed 189 final targeted backend regressions,
Ruff/format, Mypy across 177 files, frontend type checking/lint, 29 frontend tests
and backend/frontend/runtime image builds. The initial image was preloaded on
both Fedora nodes. The subsequent [distribution and recovery follow-up](command-center-recovery.md)
published a public multi-architecture GHCR image, verified direct CRI pulls on both
nodes and changed the ignored local backend configuration to that registry digest.

Real Keycloak/browser/Pod exec checks exercised target confirmation, kubectl node
reads, Helm version/list, operator denial, and denial of ordinary resource/Secret/
pod-terminal access to the runtime. End-session cleanup removed the grant and
namespace, after which the delegated credential lost authorization. Further live
checks verified browser-navigation cleanup, actual signed-token expiry/Pod UID
claims, and real Celery beat/worker reclamation with an accelerated unattached
lease. No active runtime namespaces, grants or leases remained; both Fedora nodes
and all 17 active pods were Ready and local platform readiness passed. Closed
validation leases and lifecycle audit records are retained. HA, other CNIs and
managed-cloud cluster behavior require their own live validation.

The follow-up also passed five real outage/recovery scenarios (API death,
worker/scheduler loss, Redis interruption, validation-connection outage and a
cleanup finalizer), plus 26 session/migration regressions. See the recovery guide
for the exact checks, repeatable harness and remaining boundaries.

The scoped-access/repository/transcript milestone subsequently passed 359 shared
backend regressions plus two namespaced cleanup safety cases, Mypy across 180 files
with untyped bodies checked in the new modules/routes, Ruff/format, frontend type
checking/lint, 29 frontend tests and both production image builds. Source hashes
were checked against the backend image and running frontend.

Real Keycloak/Fedora checks covered administrator assignment/revocation controls,
operator namespace selection and shell, delegated-token forbidden endpoint attempts,
default internet denial, public HTTPS index and OCI chart downloads, metadata and
unlisted destination denial, repository policy/opt-in, recording consent/status,
encrypted persistence, author/admin export, other-operator/viewer denial, UI JSON
download/deletion and real beat purge of an accelerated expired validation record.
Only owned temporary resources were changed; fixtures/recordings were removed and
the original repository policy restored. Migration `0018` is applied locally.
Platform dependencies, both Fedora nodes and all 17 active pods were healthy after
verification. These checks are local Fedora evidence, not HA/cloud certification.
