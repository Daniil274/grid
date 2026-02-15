"""
Container Manager for agent isolation using Docker.
"""

import logging
import os
import sys
from pathlib import Path
from typing import Optional, Dict, Any, Tuple
import docker
from docker.errors import NotFound, DockerException
from docker.models.containers import Container

logger = logging.getLogger("grid.container_manager")

class ContainerManager:
    """
    Manages Docker containers for agent isolation.
    Implements 'One Container per User' strategy.
    """

    def __init__(self, config: Any):
        """
        Initialize ContainerManager.
        
        Args:
            config: GridConfig object
        """
        self.config = config
        self.client = None
        self.enabled = False
        self.image = "grid-agent:latest"
        
        # Check if isolation is enabled in config
        isolation_config = getattr(config.config, "isolation", None)
        if isolation_config:
            # Handle both dict and Pydantic model
            enabled = getattr(isolation_config, "enabled", False) if not isinstance(isolation_config, dict) else isolation_config.get("enabled", False)
            if enabled:
                self.enabled = True
                self.image = getattr(isolation_config, "image", "grid-agent:latest") if not isinstance(isolation_config, dict) else isolation_config.get("image", "grid-agent:latest")
                try:
                    self.client = docker.from_env()
                    logger.info("🐳 Docker client initialized successfully")
                except DockerException as e:
                    logger.error(f"❌ Failed to initialize Docker client: {e}")
                    self.enabled = False

    def get_or_create_container(self, user_id: str) -> Optional[Container]:
        """
        Get existing container for user or create a new one.
        
        Args:
            user_id: User identifier
            
        Returns:
            Container object or None if failed
        """
        if not self.enabled or not self.client:
            return None

        container_name = f"grid-agent-{user_id}"
        
        try:
            # Try to get existing container
            container = self.client.containers.get(container_name)
            if container.status != "running":
                container.start()
            return container
        except NotFound:
            # Create new container
            return self._create_container(user_id, container_name)
        except Exception as e:
            logger.error(f"Error getting container {container_name}: {e}")
            return None

    def _create_container(self, user_id: str, container_name: str) -> Optional[Container]:
        """Create and start a new container for the user."""
        try:
            # Resolve user workspace path on host
            workspace_root = Path(self.config.get_working_directory())
            user_workspace = workspace_root / f"user_{user_id}"
            user_workspace.mkdir(parents=True, exist_ok=True)
            
            # Mounts: Host path -> Container path
            volumes = {
                str(user_workspace.absolute()): {
                    'bind': '/workspace',
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

            container = self.client.containers.run(
                self.image,
                name=container_name,
                detach=True,
                tty=True,        # Keep running
                stdin_open=True, # Keep stdin open
                volumes=volumes,
                working_dir="/workspace",
                user="agent",
                environment={"BEADS_DAEMON": "0"}, # Disable beads daemon in container
                restart_policy={"Name": "unless-stopped"},
                **run_kwargs,
            )
            return container
        except Exception as e:
            logger.error(f"Failed to create container {container_name}: {e}")
            return None

    def exec_command(self, container_id: str, cmd: list[str], workdir: str = "/workspace", env: Dict[str, str] = None) -> Tuple[int, str, str]:
        """
        Execute command in container.
        
        Args:
            container_id: Container ID or name
            cmd: Command to execute (list of strings)
            workdir: Working directory inside container
            env: Environment variables
            
        Returns:
            Tuple (exit_code, stdout, stderr)
        """
        if not self.enabled or not self.client:
            # Fallback to local execution if isolation disabled (should be handled by caller, but safety check)
            return -1, "", "Isolation disabled"

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
