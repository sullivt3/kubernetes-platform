# Fedora hosts

Conventional Fedora Server and Workstation installations on **43, 44 and 45** can
be registered over SSH and selected as control-plane or worker machines. Fedora
Rawhide/future releases and immutable OSTree systems (CoreOS/Atomic desktops) are
not accepted by this package-based installer. They need a separate image-based
provisioning path.

## Register and install

1. Store the SSH key/password under **Security → Credentials**.
2. Use **Infrastructure → Machines → Register machine**. The SSH user needs root
   or non-interactive sudo. Inspection records Fedora, DNF5, hardware and SELinux.
3. For a previously registered host with an unsupported-OS error, use its actions
   menu → **Inspect again**. This refreshes the stored checks without reinstalling
   the operating system or changing the pinned SSH host key.
4. Select the registered machines in **Kubernetes → Create cluster**. The kubeadm
   path performs the normal preparation, runtime/package installation, control-plane
   initialization, CNI installation, joins and cluster smoke test.

Minimum requirements remain the same as other Linux hosts: systemd, x86_64 or
aarch64, approximately 2 GiB RAM, two CPUs for control planes, and routing between
nodes. Package/artifact registries must be reachable. Fedora is reached through
the existing SSH provider; it does not require a new provider connection.

## Installer behavior

The containerd installer selects Docker's package repository using the OS ID:

| OS | Docker repository path |
|---|---|
| Fedora | `https://download.docker.com/linux/fedora/` |
| RHEL | `https://download.docker.com/linux/rhel/` |
| Rocky / AlmaLinux / CentOS | `https://download.docker.com/linux/centos/` |
| Ubuntu | `https://download.docker.com/linux/ubuntu/` |
| Debian | `https://download.docker.com/linux/debian/` |

Unknown RPM derivatives do not silently fall back to CentOS. Only `containerd.io`
is requested; Docker Engine is not required. The generated containerd config uses
systemd cgroups and preserves a backup of a pre-existing unmanaged configuration.

Kubernetes packages come from the selected minor's `pkgs.k8s.io` repository.
DNF5 uses `--setopt=disable_excludes=kubernetes` and machine-readable `repoquery`
output to choose an exact patch/release. DNF4 keeps its supported exclude flag.
Creation and both package-upgrade modes use the same selection logic.

Fedora preparation installs the nft-backed iptables package and accepts an
existing curl or curl-minimal installation through the executable's RPM provide.
Existing firewall handling opens the role/CNI-specific ports; the firewall is not
disabled. Network policies and other site firewall rules can still affect CNI/DNS.

Unless the cluster explicitly allows swap, the installer disables active swap,
comments swap entries in fstab, masks the captured swap units, and masks
`zram-generator` under `/etc/systemd/system-generators/`. Fedora's zram swap then
stays disabled across reboot. Vendor and user zram configuration files are kept.
The same swap helper is used by K3s/RKE2, whose existing preparation policy disables
swap. Removing a cluster does not automatically restore the host's swap settings.

The kubeadm path retains the platform's existing SELinux policy: enforcing hosts
are moved to permissive, including the boot setting. Inspection surfaces this as
a warning. K3s/RKE2 retain their separate requirement for the distribution's signed
SELinux policy on enforcing hosts; those paths do not silently disable enforcement.
The native distribution integrations remain preview.

## Validation

`backend/tests/test_linux_installers.py` executes the actual packaged Bash scripts
with command stubs and an isolated filesystem. It covers repository choice,
containerd configuration versions 2/3, retries/backups, DNF4/DNF5 install and upgrade,
repository failures, persistent swap policy, SELinux and firewall preparation.
API tests cover Fedora registration/reinspection and mixed Fedora-version
control-plane/worker creation and deletion using explicit SSH/Kubernetes simulators.

For real package validation, run:

```sh
source scripts/dev-env.sh
bash scripts/check-fedora.sh 45
```

This creates a disposable unprivileged Fedora container. DNF, RPM signature checks,
Docker/Kubernetes repositories, binaries, generated runtime config and a running
containerd CRI endpoint are real. Only systemd service control is replaced because
the image has no systemd PID 1. The check exercises installation and both package
upgrade modes. It does not contact any registered machine.

The Fedora 45 check on 2026-11-11 installed containerd **2.4.2**, installed Kubernetes
**1.36.6**, verified CRI connectivity, and upgraded the Kubernetes packages to
**1.37.2**. The backend regression run passed 343 tests; a subsequent run of all
40 final installer cases passed, including four additional K3s/RKE2 preparation
cases. Ruff, Mypy, frontend type checking/lint/tests/build, and backend wheel asset
checks passed. The registered Fedora 45 Workstation host was re-inspected through
the normal SSH workflow and became ready with DNF5 detected and no failing checks.

This is inspection/package/runtime validation, not a complete Fedora VM cluster
certification: real boot persistence, enforcing SELinux behavior, CNI and full
cluster lifecycle still require live host validation. Fedora 43/44 have automated
inspection/installer coverage; they have not had this live package check here.

References: [Docker Fedora repository](https://docs.docker.com/engine/install/fedora/),
[Docker RHEL repository](https://docs.docker.com/engine/install/rhel/),
[Kubernetes DNF4/DNF5 instructions](https://kubernetes.io/docs/setup/production-environment/tools/kubeadm/install-kubeadm/),
[systemd generator masking](https://www.freedesktop.org/software/systemd/man/latest/systemd.generator.html).
