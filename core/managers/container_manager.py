"""
Container Manager for agent isolation using Docker.
"""

import hashlib
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional, Dict, Any
try:
    import docker
    from docker.errors import NotFound, DockerException
    from docker.models.containers import Container
except ImportError:  # pragma: no cover - environment-dependent
    docker = None

    class DockerException(Exception):
        """Fallback docker exception when docker-py is unavailable."""

    class NotFound(DockerException):
        """Fallback not-found exception when docker-py is unavailable."""

    class Container:  # type: ignore[override]
        """Fallback container type used only for typing when docker is absent."""

logger = logging.getLogger("grid.container_manager")

# Path inside the container where the host workspace is mounted. Docker forbids bind to "/", so we use /workspace.
# Agent-facing paths are shown as "/"; this is the actual mount point for docker.
CONTAINER_WORKDIR = "/workspace"

#: The label naming the settings a container was made with (ContainerManager.profile).
PROFILE_LABEL = "grid.isolation.profile"

class ContainerManager:
    """
    Manages Docker containers for agent isolation.
    Implements 'One Container per User' strategy.
    """

    def __init__(self, config: Any, *, enabled: Optional[bool] = None):
        """``enabled`` None follows the config's ``isolation.enabled``; True or
        False decides regardless - a server with accounts isolates every user,
        whatever the config says. The config still gives image and limits."""
        from schemas.schemas import IsolationConfig

        self.config = config
        self.client = None
        self.enabled = False

        isolation_config = getattr(config.config, "isolation", None)
        if isinstance(isolation_config, dict):
            isolation_config = IsolationConfig(**isolation_config)
        self.settings: IsolationConfig = isolation_config or IsolationConfig()
        self.image = self.settings.image
        if not (self.settings.enabled if enabled is None else enabled):
            return
        if docker is None:
            logger.warning("Docker isolation requested but docker SDK is not installed; disabling isolation")
            return
        self.enabled = True
        try:
            self.client = docker.from_env()
            logger.info("Docker client initialized")
        except DockerException as e:
            logger.error("Failed to initialize Docker client: %s", e)
            self.enabled = False

    @property
    def profile(self) -> str:
        """A digest of the settings a container is made with. A container made
        with other settings - an older Grid, an edited config - is recreated,
        so the limits in force are always the configured ones."""
        settings = {
            "image": self.image,
            "memory": self.settings.memory,
            "cpus": self.settings.cpus,
            "pids_limit": self.settings.pids_limit,
            "hardening": ["cap_drop:ALL", "no-new-privileges", "no-restart"],
        }
        return hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()[:16]

    def get_or_create_container(self, user_id: str, workspace: Path) -> Optional[Container]:
        """The running container of *user_id*, with *workspace* - a host
        directory - mounted at CONTAINER_WORKDIR; made when missing. None
        when isolation is off or Docker fails."""
        if not self.enabled or not self.client:
            return None

        container_name = f"grid-agent-{user_id}"
        # One spelling of the host path for the mount and its check: a path
        # spelled differently would recreate the container on every call.
        workspace = Path(workspace).resolve()
        expected_host_path = str(workspace)

        try:
            container = self.client.containers.get(container_name)

            # Check that the existing container mounts the correct host path.
            # Mounts cannot be changed on a running container — recreate if mismatched.
            current_host_path = None
            for mount in container.attrs.get("Mounts", []):
                if mount.get("Destination") == CONTAINER_WORKDIR:
                    current_host_path = mount.get("Source")
                    break

            made_with = (container.attrs.get("Config", {}).get("Labels") or {}).get(PROFILE_LABEL)
            if current_host_path != expected_host_path or made_with != self.profile:
                logger.warning(
                    "Container %s mounts '%s' with settings %s; expected '%s' with %s. Recreating.",
                    container_name, current_host_path, made_with, expected_host_path, self.profile,
                )
                container.remove(force=True)
                return self._create_container(user_id, container_name, workspace=workspace)
            if not self._runs_current_image(container):
                logger.info("Container %s runs an older build of %s. Recreating.", container_name, self.image)
                container.remove(force=True)
                return self._create_container(user_id, container_name, workspace=workspace)

            if container.status != "running":
                container.start()
            return container
        except NotFound:
            # Create new container
            return self._create_container(user_id, container_name, workspace=workspace)
        except Exception as e:
            logger.error(f"Error getting container {container_name}: {e}")
            return None

    def _runs_current_image(self, container: Container) -> bool:
        """Whether *container* runs the build the image tag names now: after a
        rebuild of the image the container is recreated on the new one. Its
        workspace is a host directory, so nothing of the user's is lost."""
        try:
            return container.attrs.get("Image") == self.client.images.get(self.image).id
        except Exception as exc:  # the tag is gone or Docker does not say: keep what runs
            logger.warning("Cannot tell which build of %s container %s runs: %s", self.image, container.name, exc)
            return True

    def _create_container(self, user_id: str, container_name: str, workspace: Path) -> Optional[Container]:
        """Create and start the container of *user_id*; *workspace* is resolved."""
        try:
            workspace.mkdir(parents=True, exist_ok=True)
            # The workspace is the only host directory the container sees.
            volumes = {str(workspace): {"bind": CONTAINER_WORKDIR, "mode": "rw"}}
            logger.info("Creating container %s with workspace %s", container_name, workspace)

            # Docker Desktop (Windows/macOS) does not support network_mode="host" for Linux containers.
            # Use host networking only on Linux.
            run_kwargs: Dict[str, Any] = {}
            if sys.platform.startswith("linux"):
                run_kwargs["network_mode"] = "host"  # Allow access to host's proxy at 127.0.0.1
            else:
                # On Docker Desktop, access host services via host.docker.internal when needed.
                # Keep default bridge networking for portability.
                pass

            container_env = {"BEADS_DAEMON": "0"}
            for proxy_var in (
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "NO_PROXY",
                "ALL_PROXY",
                "http_proxy",
                "https_proxy",
                "no_proxy",
                "all_proxy",
            ):
                proxy_val = os.environ.get(proxy_var)
                if proxy_val:
                    container_env[proxy_var] = proxy_val

            # No restart policy: a user's container runs when Grid starts it for
            # that user's space, not whenever the Docker daemon comes up.
            container = self.client.containers.run(
                self.image,
                name=container_name,
                detach=True,
                tty=True,        # Keep running
                stdin_open=True, # Keep stdin open
                volumes=volumes,
                working_dir=CONTAINER_WORKDIR,
                user="agent",
                environment=container_env, # Disable beads daemon and forward proxy env vars
                labels={PROFILE_LABEL: self.profile},
                # One user's agents cannot take the machine, nor any privilege.
                mem_limit=self.settings.memory,
                nano_cpus=int(self.settings.cpus * 1e9),
                pids_limit=self.settings.pids_limit,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges=true"],
                **run_kwargs,
            )
            return container
        except Exception as e:
            logger.error(f"Failed to create container {container_name}: {e}")
            return None
