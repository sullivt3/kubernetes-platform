# Command Center distribution and recovery verification

The 2026-11-24 follow-up closes the workstation-cache dependency for interactive
sessions and verifies recovery of the initial admin-only session implementation.
It does not add scoped operator sessions, external repository egress or transcripts.

## Published runtime

[Publishing run](https://github.com/sullivt3/kubernetes-platform/actions/runs/45423664082)
built commit `b12f378dcf2adc8a37f1d26b76e4c0cea9acc97c` for Linux amd64 and arm64.
The tools-only package is anonymously readable. The verified configuration is:

```text
COMMAND_CENTER_RUNTIME_IMAGE=ghcr.io/sullivt3/kubernetes-platform/command-runtime@sha256:7393b4a2cf2247d624278ab19ddee1e3100069a023e9b4e043266da389614a20
COMMAND_CENTER_KUBECTL_MINOR=37
```

Anonymous token/manifest requests succeeded. Both pinned Fedora nodes pulled this
reference directly through CRI, without an image archive transfer. kubectl 1.37.2
and Helm 4.3.2 executed from both architecture variants in Docker (arm64 under
emulation); the Fedora live session checks cover amd64. The local API, worker and
scheduler configuration now uses this registry digest. Replacement nodes require
registry HTTPS/DNS access rather than access to the management workstation cache.
Publishing a later build does not update this configured digest automatically.

## Live drill

`scripts/check-command-recovery.py` uses the existing local Keycloak administrator
fixture and an encrypted-credential-backed temporary imported registration of the
Fedora cluster. A loopback TLS pass-through proxy lets it interrupt only that
registration's connection while preserving CA/server-name verification. It never
stops Kubernetes, changes host addressing/firewalls or modifies existing workloads.

The drill is intentionally disruptive to the local management API/worker/scheduler
and Redis. Run it only on the documented isolated test stack, with no active
operations or interactive sessions. Guards require PostgreSQL on local port 55432,
Redis on local port 56379 and the `kplatform-test` Redis container label. Do not
run regression tests concurrently with this drill: pausing Redis affects the whole
test Redis container, including otherwise separate logical databases.

```sh
source scripts/dev-env.sh
python scripts/check-command-recovery.py --cluster test-cluster --preflight-only
python scripts/check-command-recovery.py --cluster test-cluster --allow-local-test-disruptions
```

| Disruption | Verified result |
|---|---|
| Kill the API during an attached browser session | Real heartbeat timeout triggered worker cleanup without API finalization; namespace/grant disappeared and the delegated token lost access. API restarted. |
| Kill worker and scheduler, then request close | Closing state was durably recorded while cleanup could not run. Restart drained cleanup and revoked access. |
| Pause the real Redis container during close | Closing committed before broker enqueue could complete. Unpausing Redis allowed cleanup to finish without losing the lease. |
| Interrupt the temporary registration's Kubernetes connection | Cleanup remained Closing, the runtime still existed and platform liveness remained available. Restoring the connection allowed the scheduler to finish cleanup. |
| Hold the owned binding with a drill finalizer | Cleanup revoked binding subjects but preserved the finalizer and stayed Closing. The delegated token was denied. Removing only the drill finalizer allowed deletion and retry completion. |

All five scenarios passed without shortening lease/heartbeat timers. The script
restores stopped processes, unpauses Redis and removes only owned validation
resources in its `finally` block. It retains operation/audit history; the temporary
registration and its session rows are removed after verified cleanup. Ignored
`.tools/command-recovery/report.json` records successful scenarios and completion
time; process logs live beside it. A killed drill process cannot guarantee its own
restoration, so inspect service health if the harness itself is interrupted.

The final registry-backed Keycloak/browser check also passed kubectl node reads,
Helm version/list, operator and ordinary-resource bypass denial, session cleanup
and delegated-token authorization revocation. Session/migration regression tests
passed all 26 cases; Ruff, format and Python compilation checks passed for the
harness. Post-drill platform readiness was healthy, both Fedora nodes and all 17
active pods were Ready, and no active session leases, runtime namespaces, grants,
temporary registration or unfinished session preparation operations remained.

The local single-API/worker/scheduler drill does not establish HA, managed-cloud,
other-CNI or actual host-loss recovery. Pod deadlines and token expiry are separate
backstops; this drill does not wait ten minutes to prove either after total platform
loss. Observability host-loss recovery remains a separate scheduled Phase 8 item.
