# The sandbox image: one container per user of a server with accounts
# (core.managers.container_manager, isolation.image - grid-agent:latest by default).
#
# The server stays on the host; only the agents' commands run here: bash_tool,
# git and beads through `docker exec`, and MCP servers such as codegraph. The
# user's workspace is bind-mounted at /workspace. Nothing of Grid itself is in
# the image, so the build context is this directory alone:
#
#   docker build -t grid-agent:latest docker/agent
#
# The server image (the Dockerfile at the repository root) is a different image.

FROM node:26-bookworm-slim AS node-tools

ARG CODEGRAPH_VERSION=1.6.0
RUN npm install --global "@colbymchenry/codegraph@${CODEGRAPH_VERSION}"

FROM python:3.11-slim

# The user the container manager runs and execs as. 1000 matches the usual
# first user of a Linux host, so files the agents create in a bind-mounted
# workspace belong to that user there.
ARG AGENT_UID=1000
ARG AGENT_GID=1000
# CODEGRAPH_NO_DOWNLOAD: the platform bundle is installed at build time; never
# let the launcher fetch one from GitHub at run time.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    CODEGRAPH_NO_DOWNLOAD=1

RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates curl git procps ripgrep \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid "${AGENT_GID}" agent \
    && useradd --create-home --uid "${AGENT_UID}" --gid "${AGENT_GID}" --shell /bin/bash agent

# Pinned release assets with checksums, as in the server image.
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
RUN ln -s /usr/local/lib/node_modules/@colbymchenry/codegraph/npm-shim.js /usr/local/bin/codegraph \
    && codegraph --version \
    && command -v dolt \
    && command -v bd \
    && command -v rg \
    && command -v git \
    && mkdir -p /workspace \
    && chown agent:agent /workspace

USER agent
WORKDIR /workspace

# The container only waits: every command comes through `docker exec`.
CMD ["sleep", "infinity"]
