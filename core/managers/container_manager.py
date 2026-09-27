"""
Container Manager for agent isolation using Docker.
"""

import hashlib
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional, Dict, Any, Tuple
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

    def get_or_create_container(self, user_id: str, workspace: Optional[Path] = None) -> Optional[Container]:
        """
        Get existing container for user or create a new one.

        Args:
            user_id: User identifier
            workspace: Explicit workspace path. When provided it is used as-is
                       instead of computing ``workspace_root / "user_{user_id}"``.

        Returns:
            Container object or None if failed
        """
        if not self.enabled or not self.client:
            return None

        container_name = f"grid-agent-{user_id}"

        # Determine the expected host path for the container workdir
        if workspace is not None:
            expected_host_path = str(Path(workspace).resolve())
        else:
            expected_host_path = str((Path(self.config.get_working_directory()) / f"user_{user_id}").resolve())

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

            if container.status != "running":
                container.start()
            return container
        except NotFound:
            # Create new container
            return self._create_container(user_id, container_name, workspace=workspace)
        except Exception as e:
            logger.error(f"Error getting container {container_name}: {e}")
            return None

    def _create_container(self, user_id: str, container_name: str, workspace: Optional[Path] = None) -> Optional[Container]:
        """Create and start a new container for the user."""
        try:
            # Resolve user workspace path on host.
            # If an explicit workspace was provided (e.g. caller passed --path), use it directly.
            # Otherwise fall back to the legacy behaviour: append user_{user_id} to workspace root.
            if workspace is not None:
                user_workspace = Path(workspace)
            else:
                workspace_root = Path(self.config.get_working_directory())
                user_workspace = workspace_root / f"user_{user_id}"
            user_workspace.mkdir(parents=True, exist_ok=True)
            
            # Mounts: Host path -> Container path
            volumes = {
                str(user_workspace.absolute()): {
                    'bind': CONTAINER_WORKDIR,
                    'mode': 'rw'
                }
            }
            
            logger.info(f"Creating container {container_name} with workspace {user_workspace}")

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
                security_opt=["no-new-privileges:true"],
                **run_kwargs,
            )
            return container
        except Exception as e:
            logger.error(f"Failed to create container {container_name}: {e}")
            return None

    def exec_command(self, container_id: str, cmd: list[str], workdir: Optional[str] = None, env: Dict[str, str] = None) -> Tuple[int, str, str]:
        """
        Execute command in container.

        Args:
            container_id: Container ID or name
            cmd: Command to execute (list of strings)
            workdir: Working directory inside container (default: CONTAINER_WORKDIR)
            env: Environment variables
            
        Returns:
            Tuple (exit_code, stdout, stderr)
        """
        if not self.enabled or not self.client:
            # Fallback to local execution if isolation disabled (should be handled by caller, but safety check)
            return -1, "", "Isolation disabled"

        workdir = workdir or CONTAINER_WORKDIR
        try:
            container = self.client.containers.get(container_id)
            
            # docker-py exec_run returns (exit_code, output)
            # output combines stdout/stderr usually, or we can use socket
            # For simplicity using exec_run
            
            exec_result = container.exec_run(
                cmd,
                workdir=workdir,
                environment=env,
                user="agent"
            )
            
            return exec_result.exit_code, exec_result.output.decode('utf-8', errors='replace'), ""
            
        except Exception as e:
            logger.error(f"Error executing command in {container_id}: {e}")
            return -1, "", str(e)

    def get_container_id(self, user_id: str) -> Optional[str]:
        """Helper to get container ID string."""
        container = self.get_or_create_container(user_id)
        return container.id if container else None
