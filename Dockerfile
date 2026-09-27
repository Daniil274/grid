FROM node:22-bookworm-slim AS node-tools

ARG CODEGRAPH_VERSION=1.6.0
RUN npm install --global "@colbymchenry/codegraph@${CODEGRAPH_VERSION}"

FROM python:3.11-slim

ARG GRID_UID=1000
ARG GRID_GID=1000
# CODEGRAPH_NO_DOWNLOAD: the platform bundle is installed at build time; never
# let the launcher fetch one from GitHub at run time.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SEARXNG_URL=http://searxng:8080 \
    CODEGRAPH_NO_DOWNLOAD=1

RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates curl ffmpeg git procps \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid "${GRID_GID}" grid \
    && useradd --create-home --uid "${GRID_UID}" --gid "${GRID_GID}" --shell /bin/bash grid

# Pinned release assets with checksums: no install scripts from a branch and no
# GitHub API calls, which fail with 403 once the anonymous rate limit is hit.
ARG DOLT_VERSION=2.3.5
ARG DOLT_SHA256=c49d4c3e004cf1581ba0d4a00c5023a26f84eb2ec15d5fe876eed36d5343f463
ARG BEADS_VERSION=1.3.0
ARG BEADS_SHA256=2f92b904ecf35b607e44dc5c39229173af69c54f1183e8d709f1773540cdcf3b

RUN cd /tmp \
    && curl -fsSL -o dolt.tar.gz "https://github.com/dolthub/dolt/releases/download/v${DOLT_VERSION}/dolt-linux-amd64.tar.gz" \
    && echo "${DOLT_SHA256}  dolt.tar.gz" | sha256sum -c - \
    && tar -xzf dolt.tar.gz -C /tmp \
    && install -m 0755 /tmp/dolt-linux-amd64/bin/dolt /usr/local/bin/dolt \
    && curl -fsSL -o beads.tar.gz "https://github.com/gastownhall/beads/releases/download/v${BEADS_VERSION}/beads_${BEADS_VERSION}_linux_amd64.tar.gz" \
    && echo "${BEADS_SHA256}  beads.tar.gz" | sha256sum -c - \
    && tar -xzf beads.tar.gz -C /tmp bd \
    && install -m 0755 /tmp/bd /usr/local/bin/bd \
    && rm -rf /tmp/dolt.tar.gz /tmp/dolt-linux-amd64 /tmp/beads.tar.gz /tmp/bd

COPY --from=node-tools /usr/local/bin/node /usr/local/bin/node
COPY --from=node-tools /usr/local/lib/node_modules/@colbymchenry /usr/local/lib/node_modules/@colbymchenry
# The package's bin is npm-shim.js, which runs the platform bundle installed
# next to it as an optional dependency.
RUN ln -s /usr/local/lib/node_modules/@colbymchenry/codegraph/npm-shim.js /usr/local/bin/codegraph \
    && codegraph --version \
    && command -v dolt \
    && command -v bd \
    && command -v ffmpeg \
    && command -v ffprobe

WORKDIR /app
COPY . /app
RUN --mount=type=cache,target=/root/.cache/pip python -m pip install . \
    && mkdir -p /config /workspace \
    && chown -R grid:grid /config /workspace

USER grid
WORKDIR /workspace

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/', timeout=3)"

CMD ["grid-web-chat", "--routing", "/app/routing.yaml", "--path", "/workspace", "--host", "0.0.0.0", "--port", "8000"]
