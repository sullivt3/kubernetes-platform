# Disposable integration test daemon; never mounts the Docker socket or host networking.
FROM fedora:45
RUN dnf -y install firewalld dbus-daemon iproute && dnf clean all
CMD ["bash", "/check.sh"]
