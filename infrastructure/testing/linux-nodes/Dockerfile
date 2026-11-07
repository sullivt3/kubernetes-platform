# Disposable "Linux server" for testing machine onboarding and Kubernetes installation
# end to end without real hardware: Ubuntu 24.04 + systemd + OpenSSH + a sudo user.
# The platform treats these exactly like physical servers reached over SSH.
#
# NOT for production use. Runs privileged (like Kind nodes) so kubelet/containerd work.
FROM ubuntu:24.04

ENV container=docker DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      systemd systemd-sysv dbus openssh-server sudo iproute2 iptables kmod \
      ca-certificates curl gnupg procps \
 && apt-get clean && rm -rf /var/lib/apt/lists/* \
 # trim systemd units that make no sense in a container
 && find /etc/systemd/system /lib/systemd/system \
      \( -path '*.wants/*' \( -name '*getty*' -o -name '*udev*' -o -name 'systemd-remount-fs*' \) \) -delete \
 && systemctl enable ssh \
 && useradd --create-home --shell /bin/bash platform \
 && echo 'platform ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/platform && chmod 440 /etc/sudoers.d/platform \
 && mkdir -p /home/platform/.ssh && chmod 700 /home/platform/.ssh \
 && sed -i 's/^#\?PasswordAuthentication .*/PasswordAuthentication no/' /etc/ssh/sshd_config

# The public key is injected at start (see entrypoint) so no key is baked into the image.
COPY entrypoint.sh /usr/local/bin/node-entrypoint.sh
RUN chmod +x /usr/local/bin/node-entrypoint.sh

STOPSIGNAL SIGRTMIN+3
ENTRYPOINT ["/usr/local/bin/node-entrypoint.sh"]
