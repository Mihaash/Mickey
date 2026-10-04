FROM python:3.12-slim

ARG TARGETARCH
ARG KUBECTL_VERSION=stable

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl libncursesw6 \
    && if [ "$KUBECTL_VERSION" = stable ]; then KUBECTL_VERSION="$(curl -fsSL https://dl.k8s.io/release/stable.txt)"; fi \
    && ARCH="${TARGETARCH:-$(dpkg --print-architecture)}" \
    && curl -fsSLo /usr/local/bin/kubectl "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${ARCH}/kubectl" \
    && chmod +x /usr/local/bin/kubectl \
    && apt-get purge -y --auto-remove curl \
    && rm -rf /var/lib/apt/lists/*

ENV LANG=C.UTF-8
WORKDIR /opt/mickey
COPY mickey.py ./mickey.py

ENTRYPOINT ["python3", "/opt/mickey/mickey.py"]