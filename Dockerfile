FROM python:3.11-slim

ARG GRID_UID=1000
ARG GRID_GID=1000
ARG GRID_WITH_GIT=false
ARG GRID_WITH_NODE=false

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN if [ "${GRID_WITH_GIT}" = "true" ] || [ "${GRID_WITH_NODE}" = "true" ]; then apt-get update; fi \
    && if [ "${GRID_WITH_GIT}" = "true" ]; then apt-get install --no-install-recommends -y git; fi \
    && if [ "${GRID_WITH_NODE}" = "true" ]; then apt-get install --no-install-recommends -y nodejs npm; fi \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid "${GRID_GID}" grid \
    && useradd --create-home --uid "${GRID_UID}" --gid "${GRID_GID}" --shell /bin/bash grid

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
