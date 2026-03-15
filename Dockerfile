# Base image compatible with Raspberry Pi 5 (ARM64)
FROM python:3.11-slim

# Support for proxy during build and runtime
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

# Prevent Python from writing pyc files and buffering stdout
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV DEBIAN_FRONTEND=noninteractive

# Install system dependencies
# git: for git tools
# curl: for installing beads
# nodejs, npm: for MCP servers
RUN apt-get update && apt-get install -y \
    git \
    curl \
    nodejs \
    npm \
    procps \
    && rm -rf /var/lib/apt/lists/*

# Make npm resilient to slow network and keep proxy config visible for all users.
RUN npm config --global set fetch-retries 5 \
    && npm config --global set fetch-retry-factor 2 \
    && npm config --global set fetch-retry-mintimeout 20000 \
    && npm config --global set fetch-retry-maxtimeout 120000 \
    && npm config --global set fetch-timeout 300000 \
    && if [ -n "$HTTP_PROXY" ]; then npm config --global set proxy "$HTTP_PROXY"; fi \
    && if [ -n "$HTTPS_PROXY" ]; then npm config --global set https-proxy "$HTTPS_PROXY"; fi \
    && if [ -n "$NO_PROXY" ]; then npm config --global set noproxy "$NO_PROXY"; fi

# Install global NPM packages for MCP servers one by one
RUN npm install -g @modelcontextprotocol/server-filesystem
RUN npm install -g @modelcontextprotocol/server-sequential-thinking
RUN npm install -g @dillip285/mcp-terminal

# Install Dolt (required by Beads as database backend)
RUN curl -fsSL https://github.com/dolthub/dolt/releases/latest/download/install.sh | bash

# Install Beads (bd) tool
# The installer seems to install to /usr/local/bin/bd automatically in some environments
# based on the logs: "bd installed to /usr/local/bin/bd"
RUN curl -fsSL https://raw.githubusercontent.com/steveyegge/beads/main/scripts/install.sh | bash \
    && ( [ -f /usr/local/bin/bd ] || mv /root/.local/bin/bd /usr/local/bin/bd )

# Create a non-root user 'agent'
RUN useradd -m -s /bin/bash agent

# Set up workspace mount point (agent sees it as "/" in instructions)
RUN mkdir -p /workspace && chown agent:agent /workspace
WORKDIR /workspace

# Install Python dependencies
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Switch to non-root user
USER agent

# Set working directory
WORKDIR /workspace

# Default command (can be overridden)
CMD ["/bin/bash"]
