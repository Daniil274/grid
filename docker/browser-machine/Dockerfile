# A browser machine: a real Chromium on a virtual display, served to the Grid
# server by a node (grid_node). The server starts one per user who needs it
# (core.devices.machines) and connects to it on its own network.
#
# Its context is the node alone:
#
#   tar --exclude=__pycache__ -c grid_node docker/browser-machine/Dockerfile \
#     | docker build -f docker/browser-machine/Dockerfile -t grid-browser-machine:local -
#
# (with BuildKit, Dockerfile.dockerignore does the same from the repository root).
FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    DISPLAY=:99

RUN apt-get update \
    && apt-get install --no-install-recommends -y xvfb tini ca-certificates \
       fonts-liberation fonts-dejavu-core fonts-noto-core fonts-noto-color-emoji fonts-noto-cjk \
    && pip install --no-cache-dir playwright==1.63.0 mcp==1.30.0 websockets==17.1 \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* /root/.cache

# The profile directory exists, owned by the node, so a new volume mounted on it is too.
RUN useradd --create-home --uid 1000 node && install -d -o node -g node /home/node/profile
COPY grid_node /opt/grid_node/grid_node
ENV PYTHONPATH=/opt/grid_node

USER node
WORKDIR /home/node
EXPOSE 7700
ENTRYPOINT ["tini", "--", "sh", "-c", "Xvfb :99 -screen 0 1280x800x24 -nolisten tcp -nolisten unix & exec python -m grid_node --driver browser --listen 0.0.0.0:7700 --token-env NODE_TOKEN --kind browser --name \"${NODE_NAME:-browser}\" --profile /home/node/profile"]
