# Grid Agent System — Docker image
# Supports: web_chat (port 8000), timeline dashboard (port 8789)
# Compatible with Raspberry Pi 5 (ARM64)

FROM python:3.11-slim

# ── Proxy support during build and runtime ──────────────────────────────────
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY
ARG ALL_PROXY
ENV HTTP_PROXY=$HTTP_PROXY
ENV HTTPS_PROXY=$HTTPS_PROXY
ENV NO_PROXY=$NO_PROXY
ENV ALL_PROXY=$ALL_PROXY
ENV http_proxy=$HTTP_PROXY
ENV https_proxy=$HTTPS_PROXY
ENV no_proxy=$NO_PROXY
ENV all_proxy=$ALL_PROXY
ENV NPM_CONFIG_PROXY=$HTTP_PROXY
ENV NPM_CONFIG_HTTPS_PROXY=$HTTPS_PROXY
ENV npm_config_proxy=$HTTP_PROXY
ENV npm_config_https_proxy=$HTTPS_PROXY

# ── Python / system settings ────────────────────────────────────────────────
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV DEBIAN_FRONTEND=noninteractive

# ── System dependencies ─────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y \
    git \
    curl \
    nodejs \
    npm \
    procps \
    && rm -rf /var/lib/apt/lists/*

# ── npm resilience ──────────────────────────────────────────────────────────
RUN npm config --global set fetch-retries 5 \
    && npm config --global set fetch-retry-factor 2 \
    && npm config --global set fetch-retry-mintimeout 20000 \
    && npm config --global set fetch-retry-maxtimeout 120000 \
    && npm config --global set fetch-timeout 300000 \
    && if [ -n "$HTTP_PROXY" ]; then npm config --global set proxy "$HTTP_PROXY"; fi \
    && if [ -n "$HTTPS_PROXY" ]; then npm config --global set https-proxy "$HTTPS_PROXY"; fi \
    && if [ -n "$NO_PROXY" ]; then npm config --global set noproxy "$NO_PROXY"; fi

# ── Global NPM packages (MCP servers) ───────────────────────────────────────
RUN npm install -g @modelcontextprotocol/server-filesystem
RUN npm install -g @modelcontextprotocol/server-sequential-thinking
RUN npm install -g @dillip285/mcp-terminal

# ── Dolt (Beads backend) ────────────────────────────────────────────────────
RUN curl -fsSL https://github.com/dolthub/dolt/releases/latest/download/install.sh | bash

# ── Beads (bd) ──────────────────────────────────────────────────────────────
RUN curl -fsSL https://raw.githubusercontent.com/steveyegge/beads/main/scripts/install.sh | bash \
    && ( [ -f /usr/local/bin/bd ] || mv /root/.local/bin/bd /usr/local/bin/bd )

# ── Create non-root user ────────────────────────────────────────────────────
RUN useradd -m -s /bin/bash agent

# ── Workspace ───────────────────────────────────────────────────────────────
RUN mkdir -p /workspace /workspace/logs /workspace/data && chown -R agent:agent /workspace
WORKDIR /workspace

# ── Python dependencies ─────────────────────────────────────────────────────
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# ── Copy application code ───────────────────────────────────────────────────
COPY --chown=agent:agent . /workspace

# ── Expose web tool ports ───────────────────────────────────────────────────
# 8000 — web_chat (main Grid web interface)
# 8789 — timeline dashboard (agent execution traces)
EXPOSE 8000 8789

# ── Switch to non-root user ─────────────────────────────────────────────────
USER agent

# ── Default command: start web_chat ─────────────────────────────────────────
# Override with: docker run ... grid python -m timeline --host 0.0.0.0
CMD ["python", "-m", "web_chat", "--host", "0.0.0.0", "--port", "8000"]
