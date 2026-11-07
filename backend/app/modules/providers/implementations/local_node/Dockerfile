# Node image for the Local provider: a disposable "Linux server" container
# (Ubuntu 24.04 + systemd + OpenSSH + a sudo user) on the operator's workstation.
# The platform treats these exactly like servers reached over SSH. Runs privileged,
# like Kind nodes, so containerd and kubelet work inside. Not for production use.
FROM ubuntu:24.04

ENV container=docker DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      systemd systemd-sysv dbus openssh-server sudo iproute2 iptables kmod \
      ca-certificates curl gnupg procps \
 && apt-get clean && rm -rf /var/lib/apt/lists/* \
 && find /etc/systemd/system /lib/systemd/system \
      \( -path '*.wants/*' \( -name '*getty*' -o -name '*udev*' -o -name 'systemd-remount-fs*' \) \) -delete \
 && systemctl enable ssh \
 && rm -f /etc/ssh/ssh_host_* \
 && sed -i 's/^#\?PasswordAuthentication .*/PasswordAuthentication no/' /etc/ssh/sshd_config

COPY entrypoint.sh /usr/local/bin/node-entrypoint.sh
RUN chmod +x /usr/local/bin/node-entrypoint.sh

STOPSIGNAL SIGRTMIN+3
ENTRYPOINT ["/usr/local/bin/node-entrypoint.sh"]
