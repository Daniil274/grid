FROM node:22-bookworm-slim AS node-tools

RUN npm install --global @colbymchenry/codegraph

FROM python:3.11-slim

ARG GRID_UID=1000
ARG GRID_GID=1000
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SEARXNG_URL=http://searxng:8080

RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates curl ffmpeg git procps \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid "${GRID_GID}" grid \
    && useradd --create-home --uid "${GRID_UID}" --gid "${GRID_GID}" --shell /bin/bash grid

COPY --from=node-tools /usr/local/bin/node /usr/local/bin/node
COPY --from=node-tools /usr/local/lib/node_modules/@colbymchenry /usr/local/lib/node_modules/@colbymchenry
RUN ln -s /usr/local/lib/node_modules/@colbymchenry/codegraph/dist/cli.js /usr/local/bin/codegraph \
    && curl -fsSL https://github.com/dolthub/dolt/releases/latest/download/install.sh | bash \
    && curl -fsSL https://raw.githubusercontent.com/steveyegge/beads/main/scripts/install.sh | bash \
    && if [ -f /root/.local/bin/bd ]; then mv /root/.local/bin/bd /usr/local/bin/bd; fi \
    && command -v codegraph \
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
