# Build context: backend/. Publish and configure the final image by digest.
FROM python:3.13-slim-trixie AS tools
ARG TARGETARCH
COPY tools/install_helm.py tools/install_kubectl.py /tools/
RUN python /tools/install_helm.py --os linux --arch "$TARGETARCH" --destination /out/helm \
 && python /tools/install_kubectl.py --arch "$TARGETARCH" --destination /out/kubectl

FROM alpine:3.23
RUN apk add --no-cache ca-certificates \
 && addgroup -g 65532 session \
 && adduser -D -u 65532 -G session -h /home/session session
COPY --from=tools /out/helm /out/kubectl /usr/local/bin/
ENV HOME=/home/session KUBECONFIG=/credentials/kubeconfig \
    HELM_CONFIG_HOME=/home/session/config HELM_CACHE_HOME=/home/session/cache \
    HELM_DATA_HOME=/home/session/data HELM_PLUGINS=/home/session/plugins \
    HISTFILE=/dev/null TERM=xterm-256color
USER 65532:65532
WORKDIR /home/session
CMD ["/bin/sh", "-c", "sleep 600"]
