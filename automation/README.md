# Automation

## Decision: shell scripts over SSH, not Ansible (for now)

The kubeadm installer runs **idempotent bash scripts over SSH**. The scripts are packaged with the distribution in
`backend/app/modules/cluster_lifecycle/distributions/kubeadm/scripts/`, so they ship inside the backend image.

Why not Ansible?
* **Ansible cannot use Windows as a control node.** Platform workers must run anywhere the platform runs,
  including Windows development machines.
* **The scripts are small and self-contained.** Each one checks the current state before acting, so the
  orchestration engine can re-run any step after a crash or retry.
* **One fewer runtime dependency** and one fewer way for secrets to leak (Ansible facts and logs).

How the scripts handle data:
* They run as root via `sudo`.
* They receive parameters as exported environment variables on **stdin**, never on a command line.
* They stream their output into the operation log, where secrets registered by the step (bootstrap tokens,
  certificate keys, passwords) are redacted.

This directory is reserved for Ansible content if a later phase introduces Linux-only workers or user-supplied
playbooks (for example, custom OS hardening before installation).
